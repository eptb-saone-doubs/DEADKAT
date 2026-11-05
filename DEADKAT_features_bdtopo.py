"""
DEADKAT V1.1 - Module Features Anthropiques BD TOPO
=====================================================

Calcule les 18 features de contexte anthropique pour chaque dépression candidate,
à partir des 8 couches BD TOPO chargées par load_bdtopo_layers().

Principe clé : distances calculées depuis la FRONTIERE du polygone de dépression
(pas depuis le centroïde), car plus pertinent pour les grandes cuvettes.

18 features :
-------------
Bâti (4)              : dist_nearest_building_m, building_count_100m,
                         building_count_500m, nearest_building_nature
Réservoirs (2)        : dist_nearest_reservoir_m, intersects_reservoir
Routes (3)            : dist_nearest_major_road_m, dist_nearest_minor_road_m,
                         road_density_200m
Zones activité (3)    : dist_nearest_zone_activite_m, intersects_zone_activite,
                         nearest_zone_categorie
Hydro (3)             : dist_nearest_water_surface_m, intersects_water_surface,
                         water_origine
Végétation (2)        : dominant_vegetation_100m, is_in_forest
Terrain sport (1)     : dist_nearest_sport_terrain_m

Performance : utilise QgsSpatialIndex pour éviter O(N*M) sur grandes zones.

Auteur : Vincent Fister - EPTB Saône et Doubs - 2025
"""

import logging
import math

from qgis.core import (
    QgsVectorLayer,
    QgsSpatialIndex,
    QgsGeometry,
    QgsRectangle,
    QgsFeatureRequest,
    QgsPointXY
)

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


# =============================================================================
# VALEURS PAR DEFAUT (quand BD TOPO absente ou couche vide)
# =============================================================================

def _nan_value():
    """Retourne NaN si numpy dispo, sinon None (sklearn accepte les deux)."""
    return float('nan') if HAS_NUMPY else None


NA_FEATURES = {
    # Bâti
    'dist_nearest_building_m': _nan_value(),
    'building_count_100m': _nan_value(),
    'building_count_500m': _nan_value(),
    'nearest_building_nature': 'Inconnu',
    # Réservoirs
    'dist_nearest_reservoir_m': _nan_value(),
    'intersects_reservoir': 0,
    # Routes
    'dist_nearest_major_road_m': _nan_value(),
    'dist_nearest_minor_road_m': _nan_value(),
    'road_density_200m': _nan_value(),
    # Zones d'activité
    'dist_nearest_zone_activite_m': _nan_value(),
    'intersects_zone_activite': 0,
    'nearest_zone_categorie': 'Inconnu',
    # Hydro
    'dist_nearest_water_surface_m': _nan_value(),
    'intersects_water_surface': 0,
    'water_origine': 'Inconnu',
    # Végétation
    'dominant_vegetation_100m': 'Aucune',
    'is_in_forest': 0,
    # Terrain sport
    'dist_nearest_sport_terrain_m': _nan_value(),
}

# Distance maximale de recherche (m). Au-delà, on considère que l'objet est
# "loin" et on retourne une valeur élevée plutôt que de scanner tout le territoire.
MAX_SEARCH_RADIUS_M = 2000


# =============================================================================
# FONCTION 1 : construction des index spatiaux (une fois par lancement)
# =============================================================================

def build_spatial_indexes(bdtopo_layers):
    """
    Construit un QgsSpatialIndex pour chaque couche BD TOPO présente.

    V1.3 OPTIMISATION : précharge en RAM les géométries ET les attributs
    pertinents pour chaque couche. Cela évite des milliers d'appels
    layer.getFeature(fid) (seek disque GPKG) lors du calcul des features.

    Gain typique : enrichissement ~30 min -> ~30-60 s sur 600+ dépressions.

    Parameters
    ----------
    bdtopo_layers : dict
        Retour de load_bdtopo_layers().

    Returns
    -------
    dict
        {layer_key: {
            'index': QgsSpatialIndex,
            'layer': QgsVectorLayer,           # conservé pour compat descendante
            'geoms': {fid: QgsGeometry},       # cache RAM des géométries
            'attrs': {fid: {field: value}},    # cache RAM des attributs utiles
        }}
        Les clés absentes indiquent que la couche n'est pas disponible.
    """
    # Attributs à précharger par type de couche (évite de charger des champs inutiles)
    # Tout est lower-case ; on stocke après normalisation pour zapper _safe_attr.
    USEFUL_ATTRS = {
        'batiment':         ['nature'],
        'reservoir':        [],
        'troncon_route':    ['importance'],
        'surface_hydro':    ['origine', 'nature'],
        'plan_eau':         ['origine', 'nature'],
        'zone_activite':    ['categorie'],
        'zone_vegetation':  ['nature'],
        'terrain_sport':    [],
    }

    indexes = {}
    for key, layer in bdtopo_layers.items():
        if key in ('source_info',) or layer is None:
            continue
        if not isinstance(layer, QgsVectorLayer):
            continue
        try:
            # Résoudre les noms réels des champs (gère casse variable) une seule fois
            wanted = USEFUL_ATTRS.get(key, [])
            real_names = {}
            field_names_lower = {f.name().lower(): f.name() for f in layer.fields()}
            for w in wanted:
                if w in field_names_lower:
                    real_names[w] = field_names_lower[w]

            # Précharger géométries + attributs en une seule passe
            geoms = {}
            attrs = {}
            feats_for_index = []
            for feat in layer.getFeatures():
                if not feat.hasGeometry():
                    continue
                fid = feat.id()
                # Cloner la géométrie pour la détacher du provider (sinon
                # potentiellement invalidée plus tard)
                g = QgsGeometry(feat.geometry())
                geoms[fid] = g
                if real_names:
                    d = {}
                    for w, real in real_names.items():
                        try:
                            v = feat[real]
                        except (KeyError, IndexError):
                            v = None
                        if v is None:
                            d[w] = None
                        else:
                            s = str(v).strip()
                            d[w] = s if s != '' else None
                    attrs[fid] = d
                feats_for_index.append(feat)

            # Construire l'index spatial à partir des features chargées
            idx = QgsSpatialIndex()
            for feat in feats_for_index:
                idx.insertFeature(feat)

            indexes[key] = {
                'index': idx,
                'layer': layer,
                'geoms': geoms,
                'attrs': attrs,
            }
            logging.info(f"   Index spatial construit : {key} ({layer.featureCount()} features, cache RAM activé)")
        except Exception as e:
            logging.warning(f"   Impossible de construire l'index pour {key} : {e}")
    return indexes


# =============================================================================
# FONCTION 2 : helpers de recherche spatiale
# =============================================================================

def _nearest_feature_and_distance(geom_depression, spatial_info, max_radius=MAX_SEARCH_RADIUS_M):
    """
    Trouve la feature la plus proche de la frontière d'une dépression.

    V1.3 : utilise le cache RAM 'geoms' au lieu de layer.getFeature() (disque).

    Returns
    -------
    tuple (distance_m, fid) ou (None, None) si aucune feature dans le rayon
    Note : retourne le fid (pas la feature), à utiliser avec spatial_info['attrs'].
    """
    if spatial_info is None:
        return None, None

    idx = spatial_info['index']
    geoms = spatial_info.get('geoms', {})

    centroid = geom_depression.centroid().asPoint()
    search_rect = QgsRectangle(
        centroid.x() - max_radius, centroid.y() - max_radius,
        centroid.x() + max_radius, centroid.y() + max_radius
    )
    candidate_ids = idx.intersects(search_rect)

    if not candidate_ids:
        return None, None

    best_dist = float('inf')
    best_fid = None
    for fid in candidate_ids:
        g = geoms.get(fid)
        if g is None or g.isEmpty():
            continue
        d = geom_depression.distance(g)
        if d < best_dist:
            best_dist = d
            best_fid = fid

    if best_fid is None:
        return None, None
    return best_dist, best_fid


def _count_features_in_buffer(geom_depression, spatial_info, buffer_m):
    """Compte le nombre de features de la couche dans un buffer autour de la dépression."""
    if spatial_info is None:
        return 0
    buffered = geom_depression.buffer(buffer_m, 8)
    idx = spatial_info['index']
    geoms = spatial_info.get('geoms', {})
    candidate_ids = idx.intersects(buffered.boundingBox())
    count = 0
    for fid in candidate_ids:
        g = geoms.get(fid)
        if g is not None and not g.isEmpty() and buffered.intersects(g):
            count += 1
    return count


def _intersects_any(geom_depression, spatial_info):
    """Retourne 1 si la dépression intersecte ou touche une feature, 0 sinon."""
    if spatial_info is None:
        return 0
    idx = spatial_info['index']
    geoms = spatial_info.get('geoms', {})
    candidate_ids = idx.intersects(geom_depression.boundingBox())
    for fid in candidate_ids:
        g = geoms.get(fid)
        if g is not None and not g.isEmpty() and geom_depression.intersects(g):
            return 1
    return 0


def _cached_attr(spatial_info, fid, field_name, default='Inconnu'):
    """Récupère un attribut depuis le cache RAM 'attrs' (préchargé)."""
    if spatial_info is None or fid is None:
        return default
    attrs = spatial_info.get('attrs', {})
    rec = attrs.get(fid)
    if not rec:
        return default
    v = rec.get(field_name)
    if v is None or v == '':
        return default
    return v


def _safe_attr(feature, field_name, default='Inconnu'):
    """Récupère un attribut en gérant les NULL et les variations de casse.

    [LEGACY] Conservé pour compat. Préférer _cached_attr quand possible.
    """
    if feature is None:
        return default
    # Essayer plusieurs variantes de casse
    for candidate in [field_name, field_name.upper(), field_name.lower(), field_name.capitalize()]:
        try:
            val = feature[candidate]
            if val is not None and str(val).strip() != '':
                return str(val)
        except KeyError:
            continue
    return default


# =============================================================================
# FONCTION 3 : principale, calcule les 18 features
# =============================================================================

def calculate_anthropic_context_features(depression_geom, spatial_indexes):
    """
    Calcule les 18 features de contexte anthropique pour UNE dépression.

    Parameters
    ----------
    depression_geom : QgsGeometry
        Géométrie du polygone de dépression.
    spatial_indexes : dict
        Retour de build_spatial_indexes().

    Returns
    -------
    dict
        18 features (cf docstring du module).
    """
    if depression_geom is None or depression_geom.isEmpty():
        return dict(NA_FEATURES)

    features = dict(NA_FEATURES)  # copie des valeurs par défaut

    # =====================================================================
    # BATI (4 features)
    # =====================================================================
    bati_info = spatial_indexes.get('batiment')
    if bati_info is not None:
        dist, nearest_fid = _nearest_feature_and_distance(depression_geom, bati_info)
        if dist is not None:
            features['dist_nearest_building_m'] = round(dist, 2)
            features['nearest_building_nature'] = _cached_attr(
                bati_info, nearest_fid, 'nature', 'Indifferenciee'
            )

        features['building_count_100m'] = _count_features_in_buffer(depression_geom, bati_info, 100)
        features['building_count_500m'] = _count_features_in_buffer(depression_geom, bati_info, 500)

    # =====================================================================
    # RESERVOIRS (2 features)
    # =====================================================================
    reservoir_info = spatial_indexes.get('reservoir')
    if reservoir_info is not None:
        dist, _ = _nearest_feature_and_distance(depression_geom, reservoir_info)
        if dist is not None:
            features['dist_nearest_reservoir_m'] = round(dist, 2)
        features['intersects_reservoir'] = _intersects_any(depression_geom, reservoir_info)

    # =====================================================================
    # ROUTES (3 features) - distingue majeures (importance 1-3) / mineures (4-5)
    # =====================================================================
    route_info = spatial_indexes.get('troncon_route')
    if route_info is not None:
        dist_major, dist_minor = _nearest_road_by_importance(depression_geom, route_info)
        if dist_major is not None:
            features['dist_nearest_major_road_m'] = round(dist_major, 2)
        if dist_minor is not None:
            features['dist_nearest_minor_road_m'] = round(dist_minor, 2)

        # Densité routière : longueur totale des routes / surface buffer 200m
        features['road_density_200m'] = _road_density_in_buffer(depression_geom, route_info, 200)

    # =====================================================================
    # ZONES D'ACTIVITE (3 features) - potentiellement très discriminant
    # =====================================================================
    zone_info = spatial_indexes.get('zone_activite')
    if zone_info is not None:
        dist, nearest_fid = _nearest_feature_and_distance(depression_geom, zone_info)
        if dist is not None:
            features['dist_nearest_zone_activite_m'] = round(dist, 2)
            features['nearest_zone_categorie'] = _cached_attr(
                zone_info, nearest_fid, 'categorie', 'Inconnu'
            )
        features['intersects_zone_activite'] = _intersects_any(depression_geom, zone_info)

    # =====================================================================
    # HYDRO (3 features) - consolidation surface_hydro + plan_eau
    # =====================================================================
    surface_hydro_info = spatial_indexes.get('surface_hydro')
    plan_eau_info = spatial_indexes.get('plan_eau')

    # Distance min aux 2 couches combinées
    best_hydro_dist = None
    best_hydro_info = None
    best_hydro_fid = None
    for info in (surface_hydro_info, plan_eau_info):
        if info is None:
            continue
        d, fid = _nearest_feature_and_distance(depression_geom, info)
        if d is not None and (best_hydro_dist is None or d < best_hydro_dist):
            best_hydro_dist = d
            best_hydro_info = info
            best_hydro_fid = fid

    if best_hydro_dist is not None:
        features['dist_nearest_water_surface_m'] = round(best_hydro_dist, 2)
        features['water_origine'] = _derive_water_origine_cached(best_hydro_info, best_hydro_fid)

    # Intersection avec l'une des 2 couches
    intersect = 0
    for info in (surface_hydro_info, plan_eau_info):
        if info is not None and _intersects_any(depression_geom, info):
            intersect = 1
            break
    features['intersects_water_surface'] = intersect

    # =====================================================================
    # VEGETATION (2 features)
    # =====================================================================
    veg_info = spatial_indexes.get('zone_vegetation')
    if veg_info is not None:
        dominant, is_forest = _dominant_vegetation_in_buffer(depression_geom, veg_info, 100)
        features['dominant_vegetation_100m'] = dominant
        features['is_in_forest'] = is_forest

    # =====================================================================
    # TERRAIN DE SPORT (1 feature)
    # =====================================================================
    sport_info = spatial_indexes.get('terrain_sport')
    if sport_info is not None:
        dist, _ = _nearest_feature_and_distance(depression_geom, sport_info)
        if dist is not None:
            features['dist_nearest_sport_terrain_m'] = round(dist, 2)

    return features


# =============================================================================
# HELPERS SPECIALISES
# =============================================================================

def _nearest_road_by_importance(geom_depression, route_info):
    """
    Retourne (dist_majeure, dist_mineure) en scannant toutes les routes
    dans le rayon de recherche, puis en filtrant par importance.

    V1.3 : utilise le cache RAM (geoms + attrs préchargés). Plus aucun
    getFeature() disque. Le gain est massif sur les sites à grand nombre
    de routes (Chailluz : 2500+ tronçons).
    """
    idx = route_info['index']
    geoms = route_info.get('geoms', {})
    attrs = route_info.get('attrs', {})

    centroid = geom_depression.centroid().asPoint()
    search_rect = QgsRectangle(
        centroid.x() - MAX_SEARCH_RADIUS_M, centroid.y() - MAX_SEARCH_RADIUS_M,
        centroid.x() + MAX_SEARCH_RADIUS_M, centroid.y() + MAX_SEARCH_RADIUS_M
    )
    candidate_ids = idx.intersects(search_rect)

    best_major = float('inf')
    best_minor = float('inf')

    for fid in candidate_ids:
        g = geoms.get(fid)
        if g is None or g.isEmpty():
            continue
        rec = attrs.get(fid)
        if not rec:
            continue
        imp = rec.get('importance')
        if imp is None:
            continue
        # imp est déjà normalisé en str non vide par build_spatial_indexes
        d = geom_depression.distance(g)
        if imp in ('1', '2', '3'):
            if d < best_major:
                best_major = d
        elif imp in ('4', '5'):
            if d < best_minor:
                best_minor = d

    return (
        best_major if best_major != float('inf') else None,
        best_minor if best_minor != float('inf') else None
    )


def _road_density_in_buffer(geom_depression, route_info, buffer_m):
    """Densité routière = longueur totale de route dans le buffer / surface du buffer (m/m²).

    V1.3 : cache RAM pour les géométries.
    """
    buffered = geom_depression.buffer(buffer_m, 8)
    idx = route_info['index']
    geoms = route_info.get('geoms', {})
    candidate_ids = idx.intersects(buffered.boundingBox())

    total_length = 0.0
    for fid in candidate_ids:
        g = geoms.get(fid)
        if g is None or g.isEmpty():
            continue
        clipped = g.intersection(buffered)
        if not clipped.isEmpty():
            total_length += clipped.length()

    buffer_area = buffered.area()
    if buffer_area <= 0:
        return 0.0
    return round(total_length / buffer_area, 6)


def _derive_water_origine_cached(spatial_info, fid):
    """
    Déduit 'Artificielle' / 'Naturelle' / 'Inconnu' à partir des attributs
    ORIGINE et NATURE déjà cachés en RAM.
    """
    if spatial_info is None or fid is None:
        return 'Inconnu'

    origine = (_cached_attr(spatial_info, fid, 'origine', '') or '').lower()
    nature = (_cached_attr(spatial_info, fid, 'nature', '') or '').lower()

    artif_keywords = ['artificiel', 'bassin', 'gravière', 'graviere', 'carrière',
                      'carriere', 'réservoir', 'reservoir', 'retenue']
    natural_keywords = ['naturel', 'étang', 'etang', 'lac', 'marais', 'cours',
                        'rivière', 'riviere']

    combined = f"{origine} {nature}"
    if any(k in combined for k in artif_keywords):
        return 'Artificielle'
    if any(k in combined for k in natural_keywords):
        return 'Naturelle'
    return 'Inconnu'


def _derive_water_origine(feature):
    """[LEGACY] Conservé pour compat. Préférer _derive_water_origine_cached."""
    if feature is None:
        return 'Inconnu'

    origine = _safe_attr(feature, 'origine', '').lower()
    nature = _safe_attr(feature, 'nature', '').lower()

    artif_keywords = ['artificiel', 'bassin', 'gravière', 'graviere', 'carrière',
                      'carriere', 'réservoir', 'reservoir', 'retenue']
    natural_keywords = ['naturel', 'étang', 'etang', 'lac', 'marais', 'cours',
                        'rivière', 'riviere']

    combined = f"{origine} {nature}"
    if any(k in combined for k in artif_keywords):
        return 'Artificielle'
    if any(k in combined for k in natural_keywords):
        return 'Naturelle'
    return 'Inconnu'


def _dominant_vegetation_in_buffer(geom_depression, veg_info, buffer_m):
    """
    Retourne (dominant_nature, is_in_forest).
    Dominance calculée par somme des surfaces de chaque nature dans le buffer.

    V1.3 : cache RAM (geoms + attrs['nature']).
    """
    buffered = geom_depression.buffer(buffer_m, 8)
    idx = veg_info['index']
    geoms = veg_info.get('geoms', {})
    attrs = veg_info.get('attrs', {})
    candidate_ids = idx.intersects(buffered.boundingBox())

    if not candidate_ids:
        return 'Aucune', 0

    nature_areas = {}
    for fid in candidate_ids:
        g = geoms.get(fid)
        if g is None or g.isEmpty():
            continue
        clipped = g.intersection(buffered)
        if clipped.isEmpty():
            continue
        rec = attrs.get(fid) or {}
        nature = rec.get('nature') or 'Autre'
        nature_areas[nature] = nature_areas.get(nature, 0.0) + clipped.area()

    if not nature_areas:
        return 'Aucune', 0

    dominant = max(nature_areas, key=nature_areas.get)
    is_forest = 1 if 'forêt' in dominant.lower() or 'foret' in dominant.lower() else 0
    return dominant, is_forest


# =============================================================================
# TEST RAPIDE - console Python QGIS
# =============================================================================
# # 1. Prerequis : bdtopo = load_bdtopo_layers(...) deja execute
#
# exec(open(r"C:\...\calculate_anthropic_context_features.py", encoding='utf-8').read())
#
# # 2. Construire les index (une fois)
# indexes = build_spatial_indexes(bdtopo)
#
# # 3. Prendre une depression candidate quelconque
# # Par exemple depuis une couche "depressions_verifiees" chargee
# depressions_layer = QgsProject.instance().mapLayersByName("depressions_verifiees_0_5m")[0]
# first_dep = next(depressions_layer.getFeatures())
#
# # 4. Calculer les features
# features = calculate_anthropic_context_features(first_dep.geometry(), indexes)
# for k, v in features.items():
#     print(f"  {k:35s} : {v}")
# =============================================================================
