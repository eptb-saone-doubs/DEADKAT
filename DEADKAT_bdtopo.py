"""
DEADKAT V1.1 - Module BD TOPO (8 couches, WFS IGN uniquement)
==============================================================

Fonctions publiques :
    - fetch_bdtopo_layer_from_wfs() : télécharge UNE couche via WFS
    - load_bdtopo_layers()          : orchestrateur (téléchargement + ajout au projet)

Couches gérées (8) :
    E_BATI          : batiment, reservoir, terrain_sport
    A_RESEAU_ROUTIER: troncon_route
    D_HYDROGRAPHIE  : surface_hydro, plan_eau
    F_VEGETATION    : zone_vegetation
    I_ZONE_ACTIVITE : zone_activite

Source : WFS IGN Géoplateforme (https://data.geopf.fr/wfs/ows)
         Connexion internet obligatoire.

Auteur : Vincent Fister - EPTB Saône et Doubs - 2025
"""

import os
import json
import logging
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime

from qgis.core import (
    QgsVectorLayer,
    QgsVectorFileWriter,
    QgsProject,
    QgsRectangle,
    QgsFillSymbol,
    QgsLineSymbol,
    QgsLinePatternFillSymbolLayer,
    QgsSingleSymbolRenderer,
    QgsRuleBasedRenderer
)
from PyQt5.QtGui import QColor


# =============================================================================
# CONFIGURATION DES 8 COUCHES
# =============================================================================

BDTOPO_LAYERS_CONFIG = {
    # ---------------- E_BATI ----------------
    'batiment': {
        'wfs_typename': 'BDTOPO_V3:batiment',
        'gpkg_name': 'bdtopo_batiment.gpkg',
        'qgis_display_name': 'BD TOPO - Bati',
        'geometry_type': 'polygon',
        'style_type': 'fill_gray_light',
    },
    'reservoir': {
        'wfs_typename': 'BDTOPO_V3:reservoir',
        'gpkg_name': 'bdtopo_reservoir.gpkg',
        'qgis_display_name': 'BD TOPO - Reservoirs',
        'geometry_type': 'polygon',
        'style_type': 'fill_orange_light',
    },
    'terrain_sport': {
        'wfs_typename': 'BDTOPO_V3:terrain_de_sport',
        'gpkg_name': 'bdtopo_terrain_sport.gpkg',
        'qgis_display_name': 'BD TOPO - Terrains de sport',
        'geometry_type': 'polygon',
        'style_type': 'fill_yellow_light',
    },
    # ---------------- A_RESEAU_ROUTIER ----------------
    'troncon_route': {
        'wfs_typename': 'BDTOPO_V3:troncon_de_route',
        'gpkg_name': 'bdtopo_troncon_route.gpkg',
        'qgis_display_name': 'BD TOPO - Routes',
        'geometry_type': 'line',
        'style_type': 'line_routes',
    },
    # ---------------- D_HYDROGRAPHIE ----------------
    'surface_hydro': {
        'wfs_typename': 'BDTOPO_V3:surface_hydrographique',
        'gpkg_name': 'bdtopo_surface_hydro.gpkg',
        'qgis_display_name': 'BD TOPO - Surfaces hydrographiques',
        'geometry_type': 'polygon',
        'style_type': 'fill_blue_light',
    },
    'plan_eau': {
        'wfs_typename': 'BDTOPO_V3:plan_d_eau',
        'gpkg_name': 'bdtopo_plan_eau.gpkg',
        'qgis_display_name': "BD TOPO - Plans d'eau",
        'geometry_type': 'polygon',
        'style_type': 'fill_blue_light',
    },
    # ---------------- F_VEGETATION ----------------
    'zone_vegetation': {
        'wfs_typename': 'BDTOPO_V3:zone_de_vegetation',
        'gpkg_name': 'bdtopo_zone_vegetation.gpkg',
        'qgis_display_name': 'BD TOPO - Vegetation',
        'geometry_type': 'polygon',
        'style_type': 'fill_green_light',
    },
    # ---------------- I_ZONE_ACTIVITE ----------------
    'zone_activite': {
        'wfs_typename': 'BDTOPO_V3:zone_d_activite_ou_d_interet',
        'gpkg_name': 'bdtopo_zone_activite.gpkg',
        'qgis_display_name': "BD TOPO - Zones d'activite",
        'geometry_type': 'polygon',
        'style_type': 'fill_red_hatched',
    },
}

# Constantes WFS
WFS_URL = "https://data.geopf.fr/wfs/ows"
DEFAULT_CRS = "EPSG:2154"
MAX_PAGES = 50
MAX_FEATURES_PER_PAGE = 5000
HTTP_TIMEOUT = 30


# =============================================================================
# FONCTION 1 : téléchargement WFS d'UNE couche
# =============================================================================

def fetch_bdtopo_layer_from_wfs(layer_key, mnt_extent, output_folder,
                                 buffer_m=500, force_refresh=False):
    """
    Télécharge UNE couche BD TOPO via WFS Géoplateforme IGN.

    Parameters
    ----------
    layer_key : str
        Clé dans BDTOPO_LAYERS_CONFIG.
    mnt_extent : QgsRectangle
        Emprise du MNT en Lambert 93.
    output_folder : str
        Dossier de sortie (GPKG + sidecar JSON).
    buffer_m : int
        Buffer autour de l'emprise (m). Défaut 500m.
    force_refresh : bool
        Si True, ignore le cache.

    Returns
    -------
    str or None
        Chemin du GPKG, ou None si échec/couche vide.
    """
    if layer_key not in BDTOPO_LAYERS_CONFIG:
        logging.error(f"Couche inconnue : {layer_key}")
        return None

    config = BDTOPO_LAYERS_CONFIG[layer_key]
    typename = config['wfs_typename']
    output_gpkg = os.path.join(output_folder, config['gpkg_name'])
    cache_info_path = output_gpkg.replace('.gpkg', '.json')

    xmin = mnt_extent.xMinimum() - buffer_m
    ymin = mnt_extent.yMinimum() - buffer_m
    xmax = mnt_extent.xMaximum() + buffer_m
    ymax = mnt_extent.yMaximum() + buffer_m

    logging.info(f"   >> {typename}")

    # ---- Vérification cache ----
    if not force_refresh and os.path.exists(cache_info_path):
        try:
            with open(cache_info_path, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            if (cached.get('xmin', float('inf')) <= xmin and
                cached.get('ymin', float('inf')) <= ymin and
                cached.get('xmax', float('-inf')) >= xmax and
                cached.get('ymax', float('-inf')) >= ymax and
                cached.get('crs') == DEFAULT_CRS and
                cached.get('typename') == typename):
                n = cached.get('n_features', 0)
                if n > 0 and os.path.exists(output_gpkg):
                    logging.info(f"      [cache OK] {n} features")
                    return output_gpkg
                elif n == 0:
                    logging.info(f"      [cache OK] aucune feature dans cette emprise")
                    return None
        except Exception:
            pass

    # ---- Requêtes WFS paginées ----
    bbox_str = f"{xmin},{ymin},{xmax},{ymax},{DEFAULT_CRS}"
    all_features = []
    start_index = 0
    last_page_reached = False

    for page in range(1, MAX_PAGES + 1):
        params = {
            'SERVICE': 'WFS',
            'VERSION': '2.0.0',
            'REQUEST': 'GetFeature',
            'TYPENAMES': typename,
            'SRSNAME': DEFAULT_CRS,
            'BBOX': bbox_str,
            'OUTPUTFORMAT': 'application/json',
            'COUNT': str(MAX_FEATURES_PER_PAGE),
            'STARTINDEX': str(start_index)
        }
        url = f"{WFS_URL}?{urllib.parse.urlencode(params)}"

        try:
            req = urllib.request.Request(
                url,
                headers={'User-Agent': 'DEADKAT-V1.1 (EPTB Saone et Doubs)'}
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
                data = json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            logging.error(f"      HTTP {e.code} : {e.reason}")
            return None
        except urllib.error.URLError as e:
            logging.error(f"      Reseau : {e.reason}")
            return None
        except Exception as e:
            logging.error(f"      Erreur : {e}")
            return None

        features = data.get('features', [])
        n_returned = len(features)
        all_features.extend(features)

        if n_returned < MAX_FEATURES_PER_PAGE:
            last_page_reached = True
            break
        start_index += MAX_FEATURES_PER_PAGE

    if not last_page_reached:
        logging.warning(f"      Limite {MAX_PAGES} pages atteinte (resultat potentiellement tronque)")

    if not all_features:
        logging.info(f"      Aucune feature dans l'emprise")
        _write_cache_info(cache_info_path, xmin, ymin, xmax, ymax, typename, 0, buffer_m)
        return None

    # ---- Conversion GeoJSON -> GPKG ----
    temp_geojson = os.path.join(output_folder, f'_tmp_{layer_key}.geojson')
    geojson_full = {
        'type': 'FeatureCollection',
        'name': layer_key,
        'crs': {
            'type': 'name',
            'properties': {'name': f'urn:ogc:def:crs:EPSG::{DEFAULT_CRS.split(":")[1]}'}
        },
        'features': all_features
    }
    try:
        with open(temp_geojson, 'w', encoding='utf-8') as f:
            json.dump(geojson_full, f)

        src_layer = QgsVectorLayer(temp_geojson, f'{layer_key}_tmp', 'ogr')
        if not src_layer.isValid():
            logging.error(f"      GeoJSON invalide")
            return None

        save_options = QgsVectorFileWriter.SaveVectorOptions()
        save_options.driverName = 'GPKG'
        save_options.fileEncoding = 'UTF-8'
        save_options.layerName = layer_key
        save_options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile

        error = QgsVectorFileWriter.writeAsVectorFormatV3(
            src_layer, output_gpkg,
            QgsProject.instance().transformContext(), save_options
        )
        if error[0] != QgsVectorFileWriter.NoError:
            logging.error(f"      Ecriture GPKG echouee : {error}")
            return None
    except Exception as e:
        logging.error(f"      Conversion GeoJSON->GPKG : {e}")
        return None
    finally:
        try:
            if os.path.exists(temp_geojson):
                os.remove(temp_geojson)
        except Exception:
            pass

    _write_cache_info(cache_info_path, xmin, ymin, xmax, ymax, typename, len(all_features), buffer_m)
    logging.info(f"      OK ({len(all_features)} features)")
    return output_gpkg


def _write_cache_info(path, xmin, ymin, xmax, ymax, typename, n_features, buffer_m):
    """Écrit le sidecar JSON du cache."""
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({
                'xmin': xmin, 'ymin': ymin, 'xmax': xmax, 'ymax': ymax,
                'crs': DEFAULT_CRS,
                'typename': typename,
                'buffer_m': buffer_m,
                'n_features': n_features,
                'fetched_at': datetime.now().isoformat(timespec='seconds')
            }, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# =============================================================================
# FONCTION 2 : orchestrateur (télécharge + charge + ajoute au projet)
# =============================================================================

def load_bdtopo_layers(mnt_extent, output_folder, add_to_project=True, buffer_m=500):
    """
    Télécharge les 8 couches BD TOPO via WFS, crée les QgsVectorLayer,
    et optionnellement les ajoute au projet QGIS avec styles uniformes.

    Parameters
    ----------
    mnt_extent : QgsRectangle
        Emprise du MNT (Lambert 93).
    output_folder : str
        Dossier de travail DEADKAT.
    add_to_project : bool
        Si True, ajoute les couches au projet dans un groupe "BD TOPO".
    buffer_m : int
        Buffer autour de l'emprise MNT (m). Défaut 500m.

    Returns
    -------
    dict
        {
            'batiment': QgsVectorLayer or None,
            'reservoir': QgsVectorLayer or None,
            'terrain_sport': QgsVectorLayer or None,
            'troncon_route': QgsVectorLayer or None,
            'surface_hydro': QgsVectorLayer or None,
            'plan_eau': QgsVectorLayer or None,
            'zone_vegetation': QgsVectorLayer or None,
            'zone_activite': QgsVectorLayer or None,
            'source_info': dict
        }
    """
    result = {key: None for key in BDTOPO_LAYERS_CONFIG}
    result['source_info'] = {}

    logging.info("=" * 60)
    logging.info("BD TOPO - Telechargement via WFS IGN Geoplateforme")
    logging.info(f"   {len(BDTOPO_LAYERS_CONFIG)} couches a recuperer")
    logging.info(f"   Buffer emprise : {buffer_m} m")
    logging.info("=" * 60)

    # -- Fetch séquentiel des 8 couches --
    for layer_key in BDTOPO_LAYERS_CONFIG:
        gpkg = fetch_bdtopo_layer_from_wfs(layer_key, mnt_extent, output_folder, buffer_m=buffer_m)
        if gpkg:
            layer = QgsVectorLayer(
                gpkg,
                BDTOPO_LAYERS_CONFIG[layer_key]['qgis_display_name'],
                'ogr'
            )
            if layer.isValid():
                result[layer_key] = layer
                result['source_info'][layer_key] = {
                    'source': 'WFS IGN',
                    'path': gpkg,
                    'n_features': layer.featureCount()
                }

    # -- Alerte si échec complet --
    n_loaded = sum(1 for k in BDTOPO_LAYERS_CONFIG if result[k] is not None)
    if n_loaded == 0:
        logging.error("ERREUR : aucune couche BD TOPO recuperee via WFS")
        logging.error("   Verifier : connexion internet, emprise en France, disponibilite WFS IGN")
        result['source_info']['error'] = "Echec WFS complet"

    # -- Ajout au projet QGIS --
    if add_to_project and n_loaded > 0:
        _add_bdtopo_layers_to_project(result)

    # -- Résumé --
    logging.info("-" * 60)
    logging.info("BD TOPO - Resume")
    for key in BDTOPO_LAYERS_CONFIG:
        layer = result[key]
        if layer is not None:
            logging.info(f"   [OK]  {key:16s} : {layer.featureCount():6d} features")
        else:
            logging.info(f"   [--]  {key:16s} : non chargee")
    logging.info("=" * 60)

    return result


# =============================================================================
# FONCTION 3 (interne) : ajout au projet + styles uniformes
# =============================================================================

def _add_bdtopo_layers_to_project(bdtopo_result):
    """Ajoute les couches au projet dans un groupe 'BD TOPO' avec styles validés."""
    project = QgsProject.instance()
    root = project.layerTreeRoot()

    # Nettoyer groupe existant "BD TOPO"
    existing = root.findGroup("BD TOPO")
    if existing is not None:
        for child in list(existing.children()):
            try:
                lyr_id = child.layerId()
                lyr = project.mapLayer(lyr_id)
                if lyr:
                    project.removeMapLayer(lyr_id)
            except Exception:
                pass
        root.removeChildNode(existing)

    group = root.insertGroup(0, "BD TOPO")
    group.setExpanded(False)  # replié par défaut, l'utilisateur déplie s'il veut

    # Ordre d'affichage (du bas visuel vers le haut)
    # Les couches listées en premier apparaissent au-dessus dans le panneau QGIS
    display_order = [
        'batiment',          # haut (le plus discret, par-dessus le reste)
        'terrain_sport',
        'zone_activite',
        'reservoir',
        'plan_eau',
        'surface_hydro',
        'zone_vegetation',
        'troncon_route',     # bas (fond, sous les polygones)
    ]

    for layer_key in display_order:
        layer = bdtopo_result.get(layer_key)
        if layer is None:
            continue
        style_type = BDTOPO_LAYERS_CONFIG[layer_key]['style_type']
        _apply_style(layer, style_type)
        project.addMapLayer(layer, False)  # False = ne pas ajouter au root directement
        group.addLayer(layer)


# =============================================================================
# FONCTION 4 (interne) : application des styles validés
# =============================================================================

def _apply_style(layer, style_type):
    """Applique un style selon le type. Palette validee V1.1."""
    if style_type == 'fill_gray_light':
        # Bati : gris leger semi-transparent
        _style_fill_solid(layer, color='#888888', alpha=90, outline='#555555', outline_width=0.2)

    elif style_type == 'fill_orange_light':
        # Reservoirs : orange pale semi-transparent
        _style_fill_solid(layer, color='#FFA552', alpha=130, outline='#CC6600', outline_width=0.3)

    elif style_type == 'fill_yellow_light':
        # Terrain de sport : jaune pale
        _style_fill_solid(layer, color='#F5E663', alpha=110, outline='#B8A000', outline_width=0.25)

    elif style_type == 'fill_blue_light':
        # Hydro (surface_hydro et plan_eau) : bleu semi-transparent
        _style_fill_solid(layer, color='#4A90D9', alpha=130, outline='#2E5F8A', outline_width=0.3)

    elif style_type == 'fill_green_light':
        # Vegetation : vert semi-transparent
        _style_fill_solid(layer, color='#6FBF73', alpha=100, outline='#3E7A42', outline_width=0.25)

    elif style_type == 'fill_red_hatched':
        # Zone activite : rouge hachure (signal fort anthropique)
        _style_fill_hatched(layer, color='#B22222', alpha=120, hatch_angle=45)

    elif style_type == 'line_routes':
        # Routes : majeures orange epaisses, mineures grises fines
        _style_routes(layer)

    else:
        logging.warning(f"Style type inconnu : {style_type}")


def _style_fill_solid(layer, color, alpha, outline, outline_width=0.26):
    """Remplissage uni semi-transparent + contour fin."""
    rgb = QColor(color)
    symbol = QgsFillSymbol.createSimple({
        'color': f'{rgb.red()},{rgb.green()},{rgb.blue()},{alpha}',
        'outline_color': outline,
        'outline_width': str(outline_width),
        'outline_style': 'solid'
    })
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    layer.triggerRepaint()


def _style_fill_hatched(layer, color, alpha, hatch_angle=45):
    """Remplissage hachure diagonal + contour."""
    symbol = QgsFillSymbol.createSimple({
        'color': '0,0,0,0',
        'outline_color': color,
        'outline_width': '0.3',
        'outline_style': 'solid'
    })

    hatch = QgsLinePatternFillSymbolLayer()
    hatch.setAngle(hatch_angle)
    hatch.setDistance(1.2)
    hatch.setLineWidth(0.2)

    rgb = QColor(color)
    sub_line = QgsLineSymbol.createSimple({
        'color': f'{rgb.red()},{rgb.green()},{rgb.blue()},{alpha}',
        'width': '0.2'
    })
    hatch.setSubSymbol(sub_line)
    symbol.appendSymbolLayer(hatch)

    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    layer.triggerRepaint()


def _style_routes(layer):
    """Routes avec regles par importance (1-3 majeures / 4-5 mineures)."""
    default_symbol = QgsLineSymbol.createSimple({'color': '#AAAAAA', 'width': '0.26'})
    renderer = QgsRuleBasedRenderer(default_symbol)
    root_rule = renderer.rootRule()

    # Routes majeures : importance 1-3
    rule_major = root_rule.children()[0].clone()
    rule_major.setLabel("Routes majeures (importance 1-3)")
    rule_major.setFilterExpression('"importance" IN (\'1\', \'2\', \'3\')')
    rule_major.setSymbol(QgsLineSymbol.createSimple({
        'color': '#D2691E',
        'width': '0.6'
    }))

    # Routes mineures : importance 4-5 ou NULL
    rule_minor = root_rule.children()[0].clone()
    rule_minor.setLabel("Routes mineures (importance 4-5)")
    rule_minor.setFilterExpression('"importance" IN (\'4\', \'5\') OR "importance" IS NULL')
    rule_minor.setSymbol(QgsLineSymbol.createSimple({
        'color': '#AAAAAA',
        'width': '0.2'
    }))

    root_rule.removeChildAt(0)
    root_rule.appendChild(rule_major)
    root_rule.appendChild(rule_minor)

    layer.setRenderer(renderer)
    layer.triggerRepaint()


# =============================================================================
# TEST RAPIDE (console Python QGIS)
# =============================================================================
# exec(open(r"C:\...\load_bdtopo_layers_v3.py", encoding='utf-8').read())
#
# from qgis.utils import iface
# mnt = iface.activeLayer()
#
# bdtopo = load_bdtopo_layers(
#     mnt_extent=mnt.extent(),
#     output_folder=r"C:\...\OUTPUT",
#     add_to_project=True
# )
# =============================================================================
