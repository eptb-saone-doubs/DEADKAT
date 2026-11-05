"""
Script pour l'identification des dépressions fermées à partir d'un MNT sous QGIS.
Version 1.2 - HYBRIDE + ML + BD TOPO + GPKG
Auteur : Vincent Fister – 2025 - EPTB Saône et Doubs
Licence : Logiciel libre – redistribution et modification permises sans garantie.

ARCHITECTURE V1.2 :
├── Étapes 1-6 : Détection géomorphologique (DEADKAT)
├── BD TOPO    : Enrichissement contexte anthropique (18 features, WFS IGN)
├── Étape 7   : Interface cartographique d'étiquetage 3 classes
├── Étape 8   : Entraînement ML ensemble (RF)
├── Étape 9   : Application ML pour filtrage final
├── Étape 10  : Extraction points exutoires (flow accumulation)
└── Étape 11  : Identification bassins d'alimentation

NOUVEAUTÉS V1.2 :
- Migration complète Shapefile -> GeoPackage (.gpkg)
- Un seul fichier par couche (fini les 5 fichiers .shp/.shx/.dbf/.prj/.cpg)
- Support UTF-8 natif (accents corrects)
- Noms de champs longs et explicites (plus de mapping dist_bati vs dist_nearest_building_m)
- Compatibilité descendante : lecture des CSV V1.1 avec noms courts
- Enrichissement BD TOPO accéléré (~10x) via bulk writes changeAttributeValues

NOUVEAUTÉS V1.1 (intégrées) :
- BD TOPO via WFS IGN Géoplateforme (8 couches)
- 18 features ML de contexte anthropique (bâti, routes, réservoirs,
  zones d'activité, hydrographie, végétation, terrains de sport)
- BD TOPO affichée dans QGIS en groupe replié pour les modes ML

NOTE - GEOLOGIE BRGM : l'intégration de BDCharm50 a été envisagée puis RETIRÉE.
Arguments contre :
  - Faible apport marginal pour discriminer naturel/anthropique (la BD TOPO
    le fait déjà mieux via dist_bati, int_zai, dist_rtmaj).
  - Biais d'échantillonnage inévitable (étiquetage concentré en zone karstique
    -> le RF apprend "calcaire = naturel" qui n'est qu'un biais d'échantillon).
  - Couverture quaternaire qui masque le substrat et bruite le signal.
  - Coût de maintenance du WFS BRGM (instable, noms de couches changeants).
Le module DEADKAT_geologie.py est conservé en archive pour réactivation éventuelle
si un besoin empirique apparaît (dolines confondues avec bassins anthropiques).

PRÉREQUIS V1.2 :
- Connexion internet pour BD TOPO (sinon mode dégradé sans features anthropiques)
- Modules externes DEADKAT_bdtopo.py + DEADKAT_features_bdtopo.py
  dans le MÊME dossier que ce script (ou configurables via dialogue)
- QGIS 3.30+ recommandé
"""

# =============================================================================
# 1. IMPORTS ET CONFIGURATION
# =============================================================================

import os
import sys
import time
import cProfile
import pstats
import logging
import math
import gc
import glob
import subprocess
from io import StringIO
from multiprocessing import Pool, cpu_count
from concurrent.futures import ThreadPoolExecutor

# Imports QGIS
from qgis.core import (
    QgsField, QgsFeature, QgsGeometry, QgsSpatialIndex, QgsVectorLayer,
    QgsVectorFileWriter, QgsCoordinateReferenceSystem, QgsProject, QgsFeatureRequest,
    edit, QgsFields, QgsRasterLayer, QgsPointXY, QgsRectangle, QgsCoordinateTransform
)
from qgis.utils import iface
from PyQt5.QtCore import QVariant, QEventLoop, Qt, pyqtSignal
from PyQt5.QtWidgets import (QInputDialog, QFileDialog, QMessageBox, QDialog, 
                            QVBoxLayout, QHBoxLayout, QLabel, QPushButton, 
                            QTextEdit, QProgressBar, QSpinBox, QCheckBox, 
                            QGroupBox, QDockWidget, QWidget, QListWidget, 
                            QListWidgetItem, QTabWidget, QScrollArea, QGridLayout)
from PyQt5.QtGui import QFont, QColor
from processing.core.Processing import Processing
from osgeo import gdal
import processing
from scipy.stats import pearsonr

# =============================================================================
# V1.2 : CHARGEMENT DES MODULES BD TOPO (externes)
# =============================================================================
# Les deux fichiers suivants sont cherchés dans le dossier du script.
# Si absents -> une boite de dialogue demande le dossier a l'utilisateur.
#
# NOTE : l'integration de la geologie BRGM BDCharm50 a ete envisagee puis
# retiree. Raisons : faible apport marginal (la BD TOPO discrimine deja
# mieux via dist_bati, int_zai, dist_rtmaj), biais d'echantillonnage
# (etiquetage concentre en zone karstique), couverture quaternaire qui
# bruite le signal, et cout de maintenance (WFS BRGM instable).
# Le module DEADKAT_geologie.py est conserve en archive pour reactivation.

import importlib.util

BDTOPO_MODULES_LOADED = False
BDTOPO_ERROR = None
load_bdtopo_layers = None
build_spatial_indexes = None
calculate_anthropic_context_features = None
NA_FEATURES_BDTOPO = {}


def _try_load_bdtopo_modules(folder):
    """Tente de charger les modules BD TOPO depuis un dossier donné.
    Retourne True si succes, False sinon."""
    global BDTOPO_MODULES_LOADED, load_bdtopo_layers, build_spatial_indexes
    global calculate_anthropic_context_features, NA_FEATURES_BDTOPO

    bdtopo_path = os.path.join(folder, 'DEADKAT_bdtopo.py')
    features_path = os.path.join(folder, 'DEADKAT_features_bdtopo.py')

    if not (os.path.exists(bdtopo_path) and os.path.exists(features_path)):
        return False

    try:
        spec_bdtopo = importlib.util.spec_from_file_location("bdtopo_module", bdtopo_path)
        mod_bdtopo = importlib.util.module_from_spec(spec_bdtopo)
        spec_bdtopo.loader.exec_module(mod_bdtopo)
        load_bdtopo_layers = mod_bdtopo.load_bdtopo_layers

        spec_feat = importlib.util.spec_from_file_location("features_module", features_path)
        mod_feat = importlib.util.module_from_spec(spec_feat)
        spec_feat.loader.exec_module(mod_feat)
        build_spatial_indexes = mod_feat.build_spatial_indexes
        calculate_anthropic_context_features = mod_feat.calculate_anthropic_context_features
        NA_FEATURES_BDTOPO = mod_feat.NA_FEATURES

        BDTOPO_MODULES_LOADED = True
        return True
    except Exception as e:
        print(f"Erreur chargement depuis {folder} : {e}")
        return False


    return True


# =============================================================================
# HELPERS BD TOPO (detection/chargement lazy - appel differe)
# Strategie : config JSON persistant + dialogue parcourir AU BESOIN
# Les modules ne sont PAS charges au demarrage : le chargement est declenche
# uniquement si l'utilisateur active BD TOPO dans un mode compatible.
# =============================================================================

import json as _json

def _get_deadkat_config_path():
    """Chemin du fichier de config DEADKAT dans le dossier utilisateur."""
    home = os.path.expanduser("~")
    config_dir = os.path.join(home, ".deadkat")
    if not os.path.exists(config_dir):
        try:
            os.makedirs(config_dir)
        except Exception:
            pass
    return os.path.join(config_dir, "DEADKAT_config.json")


def _load_deadkat_config():
    """Lit le fichier de config JSON, retourne un dict (vide si absent)."""
    path = _get_deadkat_config_path()
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return _json.load(f)
        except Exception:
            return {}
    return {}


def _save_deadkat_config(cfg):
    """Sauvegarde le dict dans le fichier de config JSON."""
    path = _get_deadkat_config_path()
    try:
        with open(path, 'w', encoding='utf-8') as f:
            _json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Erreur sauvegarde config : {e}")


def ensure_bdtopo_modules_loaded():
    """
    Charge les modules BD TOPO au besoin (appel lazy).
    Retourne True si chargement reussi, False sinon.

    Strategie :
    1) Tentative depuis la config JSON (chemin memorise)
    2) Tentative depuis le dossier courant
    3) QFileDialog directement pour choisir le dossier
    """
    global BDTOPO_MODULES_LOADED, BDTOPO_ERROR

    # Deja charge : rien a faire
    if BDTOPO_MODULES_LOADED:
        return True

    # 1) Config JSON (chemin deja memorise)
    cfg = _load_deadkat_config()
    saved_folder = cfg.get('bdtopo_modules_folder')
    if saved_folder and _try_load_bdtopo_modules(saved_folder):
        print(f"BD TOPO modules charges depuis {saved_folder} (config)")
        return True

    # 2) Dossier courant
    fallback_dir = os.getcwd()
    if _try_load_bdtopo_modules(fallback_dir):
        print(f"BD TOPO modules charges depuis {fallback_dir}")
        cfg['bdtopo_modules_folder'] = fallback_dir
        _save_deadkat_config(cfg)
        return True

    # 3) Dialogue direct de selection de dossier
    try:
        from qgis.PyQt.QtWidgets import QFileDialog

        chosen = QFileDialog.getExistingDirectory(
            None,
            "Dossier contenant DEADKAT_bdtopo.py et DEADKAT_features_bdtopo.py",
            fallback_dir
        )
        if chosen and _try_load_bdtopo_modules(chosen):
            print(f"BD TOPO modules charges depuis {chosen}")
            cfg['bdtopo_modules_folder'] = chosen
            _save_deadkat_config(cfg)
            return True
        else:
            BDTOPO_ERROR = "Modules BD TOPO introuvables dans le dossier selectionne"
            print(f"Attention : {BDTOPO_ERROR}")
            return False
    except Exception as e:
        BDTOPO_ERROR = f"Erreur dialogue chargement : {e}"
        print(f"Attention : {BDTOPO_ERROR}")
        return False

# V1.2 : Noms longs natifs (GPKG permet UTF-8 + noms longs sans limite)
# Le mapping long_name -> short_name est conserve pour la lecture
# retrocompatible des anciens CSV/shapefiles (V1.1) qui auraient des noms courts.
BDTOPO_FIELD_MAPPING = {
    'dist_nearest_building_m': 'dist_nearest_building_m',
    'building_count_100m': 'building_count_100m',
    'building_count_500m': 'building_count_500m',
    'nearest_building_nature': 'nearest_building_nature',
    'dist_nearest_reservoir_m': 'dist_nearest_reservoir_m',
    'intersects_reservoir': 'intersects_reservoir',
    'dist_nearest_major_road_m': 'dist_nearest_major_road_m',
    'dist_nearest_minor_road_m': 'dist_nearest_minor_road_m',
    'road_density_200m': 'road_density_200m',
    'dist_nearest_zone_activite_m': 'dist_nearest_zone_activite_m',
    'intersects_zone_activite': 'intersects_zone_activite',
    'nearest_zone_categorie': 'nearest_zone_categorie',
    'dist_nearest_water_surface_m': 'dist_nearest_water_surface_m',
    'intersects_water_surface': 'intersects_water_surface',
    'water_origine': 'water_origine',
    'dominant_vegetation_100m': 'dominant_vegetation_100m',
    'is_in_forest': 'is_in_forest',
    'dist_nearest_sport_terrain_m': 'dist_nearest_sport_terrain_m',
}

# Mapping noms courts V1.1 -> noms longs V1.2 (pour lecture retrocompatible CSV V1.1)
LEGACY_SHORT_TO_LONG = {
    'dist_bati': 'dist_nearest_building_m',
    'bati_100m': 'building_count_100m',
    'bati_500m': 'building_count_500m',
    'bati_nat': 'nearest_building_nature',
    'dist_res': 'dist_nearest_reservoir_m',
    'int_res': 'intersects_reservoir',
    'dist_rtmaj': 'dist_nearest_major_road_m',
    'dist_rtmin': 'dist_nearest_minor_road_m',
    'rt_dens': 'road_density_200m',
    'dist_zai': 'dist_nearest_zone_activite_m',
    'int_zai': 'intersects_zone_activite',
    'zai_cat': 'nearest_zone_categorie',
    'dist_hydro': 'dist_nearest_water_surface_m',
    'int_hydro': 'intersects_water_surface',
    'hydro_orig': 'water_origine',
    'veg_dom': 'dominant_vegetation_100m',
    'in_forest': 'is_in_forest',
    'dist_sport': 'dist_nearest_sport_terrain_m',
}

BDTOPO_NUMERIC_SHORT = [
    'dist_nearest_building_m', 'building_count_100m', 'building_count_500m',
    'dist_nearest_reservoir_m', 'intersects_reservoir',
    'dist_nearest_major_road_m', 'dist_nearest_minor_road_m', 'road_density_200m',
    'dist_nearest_zone_activite_m', 'intersects_zone_activite',
    'dist_nearest_water_surface_m', 'intersects_water_surface',
    'is_in_forest', 'dist_nearest_sport_terrain_m'
]

BDTOPO_CATEGORICAL_SHORT = [
    'nearest_building_nature', 'nearest_zone_categorie',
    'water_origine', 'dominant_vegetation_100m'
]

BDTOPO_ALL_SHORT = BDTOPO_NUMERIC_SHORT + BDTOPO_CATEGORICAL_SHORT

global_dock_widget = None

# =============================================================================
# 2. CONFIGURATION LOGGING SIMPLE
# =============================================================================

def setup_simple_logging(output_folder):
    """Configuration logging simple"""
    # Supprimer anciens handlers
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
    
    # Configuration basique
    log_format = '%(asctime)s - [%(levelname)s] - %(message)s'
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        datefmt='%H:%M:%S',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(os.path.join(output_folder, 'journal.log'), mode='w', encoding='utf-8')
        ]
    )
    logging.info(f"Logging configuré - Fichier: {os.path.join(output_folder, 'journal.log')}")

# =============================================================================
# 3. VÉRIFICATION ET IMPORTS CONDITIONNELS
# =============================================================================

# Variables de disponibilité des modules
HAS_NUMPY = False
HAS_PANDAS = False
HAS_JENKSPY = False
HAS_SKLEARN = False

# Vérification NumPy
try:
    import numpy as np
    HAS_NUMPY = True
    print("NumPy disponible - Calculs optimisés activés")
except ImportError:
    print("NumPy non disponible - Mode dégradé activé")

# Vérification Pandas
try:
    import pandas as pd
    HAS_PANDAS = True
    print("Pandas disponible - Tableaux avancés activés")
except ImportError:
    print("Pandas non disponible - Tableaux simplifiés")

# Vérification Jenkspy
try:
    import jenkspy
    HAS_JENKSPY = True
    print("Jenkspy disponible - Classification Jenks optimisée")
except ImportError:
    print("Jenkspy non disponible - Classification native")

# Vérification Scikit-learn
try:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.model_selection import train_test_split, cross_val_score
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import classification_report, confusion_matrix
    import joblib
    HAS_SKLEARN = True
    print("Scikit-learn disponible - Machine Learning activé")
except ImportError:
    print("Scikit-learn non disponible - ML désactivé")

# Configuration du logging initial
logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')

# =============================================================================
# 4. VARIABLES GLOBALES
# =============================================================================

script_aborted = False
cuvettes_verified_layer = None
global_dock_widget = None  

# =============================================================================
# 5. FONCTIONS UTILITAIRES DE BASE
# =============================================================================

def to_safe_str(value):
    """Remplace les points par des underscores pour des noms de fichiers sûrs"""
    return str(value).replace('.', '_')

def safe_get_attribute(feature, field_name, default_value=0):
    """Version avec gestion virgule décimale"""
    try:
        value = feature[field_name]
        if value is not None:
            # Gestion spéciale des virgules décimales
            if isinstance(value, str) and ',' in str(value):
                try:
                    return float(str(value).replace(',', '.'))
                except ValueError:
                    return default_value
            return value
        return default_value
    except:
        return default_value

def get_adaptive_parameters(resolution_value, base_min_diff):
    """Calcule les paramètres adaptatifs selon la résolution MNT"""
    buffer_distance = max(resolution_value * 2.5, 1.0)
    min_valid_neighbors = max(2, int(buffer_distance / resolution_value))
    envelope_validation_ratio = 0.5  
    
    return {
        'min_diff': base_min_diff,
        'buffer_distance': buffer_distance,
        'min_valid_neighbors': min_valid_neighbors,
        'envelope_validation_ratio': envelope_validation_ratio
    }

def cleanup_unwanted_training_files(output_folder):
    """Supprime SEULEMENT les anciens fichiers V0 (système 2 classes avec vrais_positifs)"""
    unwanted_files = [
        'vrais_positifs_training.csv',    # ← Ancien système V0
        'vrais_positifs_training.shp'     # ← Ancien système V0
    ]
    
    for filename in unwanted_files:
        filepath = os.path.join(output_folder, filename)
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
                # Supprimer aussi les fichiers associés pour les shapefiles
                if filename.endswith('.shp'):
                    for ext in ['.shx', '.dbf', '.prj', '.cpg']:
                        aux_file = filepath.replace('.shp', ext)
                        if os.path.exists(aux_file):
                            os.remove(aux_file)
                logging.info(f"📦 Ancien fichier V0 supprimé : {filename}")
            except Exception as e:
                logging.warning(f"Impossible de supprimer {filename}: {e}")


def cleanup_intermediate_layers_from_project(resolution, mode="training"):
    """
    Supprime les couches intermédiaires du projet QGIS selon le mode
    
    Args:
        resolution: Résolution du MNT
        mode: "training" → supprime verified + depressions (garde training + 3 shapefiles étiquetés)
              "classic" → ne supprime rien (garde verified + depressions pour traçabilité)
    """
    if mode == "classic":
        logging.info("ℹ️ Mode classique : conservation des couches intermédiaires")
        return
    
    try:
        project = QgsProject.instance()
        safe_res = to_safe_str(resolution)
        
        # Couches à nettoyer en mode training uniquement
        intermediate_names = [
            f"depressions_verifiees_{safe_res}m",  # Sortie brute étape 7
            f"depressions_{safe_res}m"            # Après regroupement
        ]
        
        removed_count = 0
        for layer_name in intermediate_names:
            layers = project.mapLayersByName(layer_name)
            for layer in layers:
                project.removeMapLayer(layer.id())
                removed_count += 1
                logging.info(f"🗑️ Couche supprimée du projet : {layer_name}")
        
        if removed_count > 0:
            logging.info(f"✅ {removed_count} couche(s) intermédiaire(s) nettoyée(s)")
            logging.info("ℹ️ Les fichiers restent sur disque pour traçabilité")
        else:
            logging.info("ℹ️ Aucune couche intermédiaire à nettoyer")
        
    except Exception as e:
        logging.warning(f"⚠️ Nettoyage partiel des couches : {str(e)}")


def show_requirements():
    """Affiche les prérequis avec une interface améliorée et redimensionnable"""
    
    # Créer la boîte de dialogue principale
    dialog = QDialog()
    dialog.setWindowTitle("DEADKAT V1.0 - Prérequis et Installation")
    dialog.setMinimumSize(800, 600)
    dialog.resize(900, 700)
    
    # Layout principal
    main_layout = QVBoxLayout(dialog)
    
    # Titre principal
    title_label = QLabel("PRÉREQUIS NÉCESSAIRES POUR DEADKAT V1.0")
    title_font = QFont()
    title_font.setPointSize(14)
    title_font.setBold(True)
    title_label.setFont(title_font)
    title_label.setAlignment(Qt.AlignCenter)
    title_label.setStyleSheet("QLabel { color: #d32f2f; margin: 10px; }")
    main_layout.addWidget(title_label)
    
    # Créer des onglets pour organiser l'information
    tab_widget = QTabWidget()
    
    # ONGLET 1: LOGICIELS REQUIS
    software_widget = QWidget()
    software_layout = QVBoxLayout(software_widget)
    
    software_group = QGroupBox("LOGICIELS REQUIS")
    software_group_layout = QVBoxLayout(software_group)
    
    software_text = QTextEdit()
    software_text.setReadOnly(True)
    software_text.setMaximumHeight(150)
    software_text.setHtml("""
    <ul>
    <li><b>QGIS 3.16+</b> (version LTR recommandée)</li>
    <li><b>Extension GRASS GIS</b> activée dans QGIS</li>
    </ul>
    """)
    software_group_layout.addWidget(software_text)
    software_layout.addWidget(software_group)
    
    # SECTION MODULES PYTHON
    modules_group = QGroupBox("MODULES PYTHON")
    modules_layout = QGridLayout(modules_group)
    
    # Modules nécessaires
    necessary_label = QLabel("<b>NÉCESSAIRES :</b>")
    modules_layout.addWidget(necessary_label, 0, 0)
    
    necessary_text = QTextEdit()
    necessary_text.setReadOnly(True)
    necessary_text.setMaximumHeight(100)
    necessary_text.setHtml("""
    <ul>
    <li><b>pandas</b> - création des tableaux de contingence</li>
    <li><b>jenkspy</b> - classification de Jenks</li>
    <li><b>scikit-learn</b> - Machine Learning</li>
    <li><b>joblib</b> - sauvegarde modèles ML</li>
    </ul>
    """)
    modules_layout.addWidget(necessary_text, 1, 0)
    
    # Modules optionnels
    optional_label = QLabel("<b>OPTIONNELS :</b>")
    modules_layout.addWidget(optional_label, 0, 1)
    
    optional_text = QTextEdit()
    optional_text.setReadOnly(True)
    optional_text.setMaximumHeight(100)
    optional_text.setHtml("""
    <ul>
    <li><b>numpy</b> - améliore les performances</li>
    <li><b>autres modules</b> - ensemble ML avancé</li>
    </ul>
    """)
    modules_layout.addWidget(optional_text, 1, 1)
    
    software_layout.addWidget(modules_group)
    tab_widget.addTab(software_widget, "Prérequis")
    
    # ONGLET 2: INSTALLATION
    install_widget = QWidget()
    install_layout = QVBoxLayout(install_widget)
    
    install_group = QGroupBox("INSTALLATION MODULES PYTHON")
    install_group_layout = QVBoxLayout(install_group)
    
    install_text = QTextEdit()
    install_text.setReadOnly(True)
    install_text.setPlainText("""MÉTHODE COMPLÈTE (recommandée) :

1. Ouvrir QGIS → Extensions → Console Python (ou Ctrl+Alt+P)

2. Coller et exécuter ce code :

import subprocess
import sys

modules = ["pandas", "jenkspy", "scikit-learn", "joblib", "numpy"]
for module in modules:
    try:
        print(f"Installation de {module}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", module])
        print(f"✅ {module} installé avec succès")
    except Exception as e:
        print(f"❌ Erreur pour {module}: {e}")
print("Installation terminée ! Redémarrez QGIS.")

3. Redémarrer QGIS""")
    
    install_group_layout.addWidget(install_text)
    install_layout.addWidget(install_group)
    
    # Vérification
    verif_group = QGroupBox("VÉRIFICATION DE L'INSTALLATION")
    verif_layout = QVBoxLayout(verif_group)
    
    verif_text = QTextEdit()
    verif_text.setReadOnly(True)
    verif_text.setMaximumHeight(80)
    verif_text.setPlainText("""Dans la console QGIS, taper :

import pandas, jenkspy, sklearn, joblib, numpy
print("✅ Tous les modules sont installés !")""")
    
    verif_layout.addWidget(verif_text)
    install_layout.addWidget(verif_group)
    
    tab_widget.addTab(install_widget, "Installation")
    
    # ONGLET 3: WORKFLOW
    workflow_widget = QWidget()
    workflow_layout = QVBoxLayout(workflow_widget)
    
    workflow_text = QTextEdit()
    workflow_text.setReadOnly(True)
    workflow_text.setHtml("""
    <h3>WORKFLOW DEADKAT V1.0</h3>
    
    <h4>ANALYSE CLASSIQUE :</h4>
    <ul>
    <li><b>OPTION 1</b> → Générer les dépressions uniquement</li>
    <li><b>OPTION 2</b> → Générer les dépressions + bassins associés</li>
    </ul>
    
    <h4> OPTION 3 : MACHINE LEARNING :</h4>
    <ul>
    <li><b>ÉTAPE 1</b> → Créer données d'entraînement (interface étiquetage)</li>
    <li><b>ÉTAPE 2</b> → Entraîner modèle ensemble (Random Forest)</li>
    <li><b>ÉTAPE 3a</b> → Appliquer modèle (dépressions seules)</li>
    <li><b>ÉTAPE 3b</b> → Appliquer modèle (dépressions seules + bassins)</li>
    </ul>
    """)
    
    workflow_layout.addWidget(workflow_text)
    tab_widget.addTab(workflow_widget, "Workflow")
    
    # Ajouter les onglets au layout principal
    main_layout.addWidget(tab_widget)
    
    # Boutons
    button_layout = QHBoxLayout()
    button_layout.addStretch()
    
    cancel_button = QPushButton("Annuler")
    cancel_button.setStyleSheet("QPushButton { background-color: #f44336; color: white; padding: 8px 16px; }")
    
    continue_button = QPushButton("Continuer")
    continue_button.setStyleSheet("QPushButton { background-color: #4CAF50; color: white; padding: 8px 16px; }")
    continue_button.setDefault(True)
    
    button_layout.addWidget(cancel_button)
    button_layout.addWidget(continue_button)
    main_layout.addLayout(button_layout)
    
    # Connexions
    result = None
    
    def accept():
        nonlocal result
        result = True
        dialog.accept()
    
    def reject():
        nonlocal result  
        result = False
        dialog.reject()
    
    continue_button.clicked.connect(accept)
    cancel_button.clicked.connect(reject)
    
    # Afficher la boîte de dialogue
    dialog.exec_()
    
    if not result:
        raise Exception("Script annulé par l'utilisateur")
    
    logging.info("Prérequis vérifiés - Lancement du script")

def select_workflow():
    """Sélection du workflow avec numérotation cohérente"""
    
    dialog = QDialog()
    dialog.setWindowTitle("DEADKAT V1.0 - Sélection du workflow")
    dialog.setMinimumSize(700, 500)
    dialog.resize(800, 600)
    
    main_layout = QVBoxLayout(dialog)
    
    # Titre
    title_label = QLabel("SÉLECTION DU WORKFLOW")
    title_font = QFont()
    title_font.setPointSize(14)
    title_font.setBold(True)
    title_label.setFont(title_font)
    title_label.setAlignment(Qt.AlignCenter)
    main_layout.addWidget(title_label)
    
    # Liste des workflows
    workflow_list = QListWidget()
    workflow_list.setStyleSheet("""
        QListWidget {
            font-size: 12pt;
            padding: 5px;
        }
        QListWidget::item {
            padding: 10px;
            border-bottom: 1px solid #ddd;
        }
        QListWidget::item:selected {
            background-color: #2196F3;
            color: white;
        }
        QListWidget::item:hover {
            background-color: #e3f2fd;
        }
    """)
    
    workflows = [
        "OPTION 1 - Générer les dépressions",
        "OPTION 2 - Générer les dépressions + bassins associés",
        "OPTION 3 - MACHINE LEARNING",
        "  → ÉTAPE 1 - Créer données d'entraînement",
        "  → ÉTAPE 2 - Entraîner modèle ensemble (RF)",
        "  → ÉTAPE 3a - Appliquer modèle (dépressions seules)",
        "  → ÉTAPE 3b - Appliquer modèle (dépressions + bassins)"
    ]
    
    descriptions = {
        "OPTION 1 - Générer les dépressions": 
            "Analyse géomorphologique classique\n"
            "Génère : depressions_[X]m.gpkg",
        
        "OPTION 2 - Générer les dépressions + bassins associés":
            "Analyse géomorphologique + hydrologie\n"
            "Génère : dépressions + bassins d'alimentation\n"
            "Interface d'ajustement des points exutoires",
        
        "OPTION 3 - MACHINE LEARNING":
            "Workflow complet en 3 étapes pour utiliser le Machine Learning\n"
            "Sélectionnez l'étape appropriée ci-dessous",
        
        "  → ÉTAPE 1 - Créer données d'entraînement":
            "Détecte les dépressions puis lance l'interface d'étiquetage\n"
            "Génère : vrais_positifs_training.csv + faux_positifs_training.csv\n"
            "Étape préalable obligatoire pour utiliser le ML",
        
        "  → ÉTAPE 2 - Entraîner modèle ensemble (RF)":
            "Utilise vos fichiers CSV pour créer un modèle Random Forest\n"
            "Génère : modele_rf.joblib (modèle prêt)\n"
            "Nécessite d'avoir créé des données d'entraînement (ÉTAPE 1)",
        
        "  → ÉTAPE 3a - Appliquer modèle (dépressions seules)":
            "Utilise votre modèle entraîné pour filtrer automatiquement\n"
            "Génère : depressions_ml_validated.gpkg (résultat filtré)\n"
            "Nécessite un modèle ML existant (ÉTAPE 2)",
        
        "  → ÉTAPE 3b - Appliquer modèle (dépressions + bassins)":
            "Combine ML + analyse hydrologique complète\n"
            "Génère : dépressions validées + bassins d'alimentation\n"
            "Nécessite un modèle ML existant (ÉTAPE 2)"
    }
    
    # Mapping vers les noms internes du code
    internal_mapping = {
        "OPTION 1 - Générer les dépressions": "🔍 DÉTECTER des dépressions",
        "OPTION 2 - Générer les dépressions + bassins associés": "💧 DÉTECTER des dépressions + bassins associés",
        "OPTION 3 - MACHINE LEARNING": None,
        "  → ÉTAPE 1 - Créer données d'entraînement": "🏷️ CRÉER des données d'entraînement ML",
        "  → ÉTAPE 2 - Entraîner modèle ensemble (RF)": "🧠 ENTRAÎNER un modèle ML",
        "  → ÉTAPE 3a - Appliquer modèle (dépressions seules)": "🤖 APPLIQUER un modèle ML (dépressions)",
        "  → ÉTAPE 3b - Appliquer modèle (dépressions + bassins)": "🤖💧 APPLIQUER un modèle ML (dépressions + bassins)"
    }
    
    for workflow in workflows:
        item = QListWidgetItem(workflow)
        if workflow == "OPTION 3 - MACHINE LEARNING":
            font = QFont()
            font.setBold(True)
            item.setFont(font)
            item.setFlags(Qt.ItemIsEnabled)
        workflow_list.addItem(item)
    
    workflow_list.setCurrentRow(0)
    main_layout.addWidget(workflow_list)
    
    # Zone de description
    description_text = QTextEdit()
    description_text.setReadOnly(True)
    description_text.setMaximumHeight(120)
    description_text.setStyleSheet("QTextEdit { background-color: #f5f5f5; padding: 10px; }")
    description_text.setPlainText(descriptions[workflows[0]])
    main_layout.addWidget(description_text)
    
    def update_description():
        selected = workflow_list.currentItem()
        if selected:
            description_text.setPlainText(descriptions[selected.text()])
    
    workflow_list.currentItemChanged.connect(update_description)
    
    # Boutons
    button_layout = QHBoxLayout()
    button_layout.addStretch()
    
    cancel_button = QPushButton("Annuler")
    cancel_button.setStyleSheet("QPushButton { background-color: #f44336; color: white; padding: 8px 20px; }")
    
    select_button = QPushButton("Sélectionner")
    select_button.setStyleSheet("QPushButton { background-color: #4CAF50; color: white; padding: 8px 20px; }")
    select_button.setDefault(True)
    
    button_layout.addWidget(cancel_button)
    button_layout.addWidget(select_button)
    main_layout.addLayout(button_layout)
    
    selected_workflow = None
    
    def accept():
        nonlocal selected_workflow
        current_item = workflow_list.currentItem()
        display_name = current_item.text()
        
        if internal_mapping.get(display_name) is None:
            QMessageBox.warning(dialog, "Sélection invalide", 
                              "Veuillez sélectionner une étape spécifique, pas le titre de section.")
            return
        
        selected_workflow = internal_mapping[display_name]
        dialog.accept()
    
    def reject():
        dialog.reject()
    
    select_button.clicked.connect(accept)
    cancel_button.clicked.connect(reject)
    
    result = dialog.exec_()
    
    if result == QDialog.Accepted and selected_workflow:
        return selected_workflow, True
    else:
        return None, False

# =============================================================================
# 6. FONCTIONS DE PROFILING
# =============================================================================

def profile_start():
    """Démarre le profiling des performances"""
    pr = cProfile.Profile()
    pr.enable()
    return pr

def profile_save(pr, output_folder):
    """Sauvegarde les résultats du profiling"""
    pr.disable()
    s = StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats('cumulative')
    ps.print_stats()
    profiling_output = os.path.join(output_folder, 'performances.txt')
    with open(profiling_output, 'w') as f:
        f.write(s.getvalue())
    logging.info("Profiling terminé et sauvegardé dans %s", profiling_output)

# =============================================================================
# 7. RÉCUPÉRATION DES PARAMÈTRES UTILISATEUR
# =============================================================================

def get_mnt_processing_parameters(mnt_path, output_folder, resolution_value, is_labeling_mode=False):
    """Collecte les paramètres de traitement MNT une seule fois pour éviter la redondance"""
    
    # Validation du MNT
    mnt_dataset = gdal.Open(mnt_path)
    if mnt_dataset is None or mnt_dataset.RasterXSize == 0 or mnt_dataset.RasterYSize == 0:
        raise Exception(f"[ERREUR] Le fichier MNT {mnt_path} est invalide ou vide.")
    geo = mnt_dataset.GetGeoTransform()
    mnt_extent = (
        geo[0],
        geo[0] + geo[1] * mnt_dataset.RasterXSize,
        geo[3] + geo[5] * mnt_dataset.RasterYSize,
        geo[3]
    )
    logging.info("Projection et étendue du MNT validées : %s", mnt_extent)

    # Option de lissage médian
    smoothing_choice, ok = QInputDialog.getItem(
        None,
        "Intensité du lissage",
        "Sélectionnez l'intensité du lissage médian :\n• Aucun : pas de lissage\n" +
        "• Faible : fenêtre 3×3 pixels (lissage léger)\n" +
        "• Moyen : fenêtre 5×5 pixels (équilibre)\n" +
        "• Fort : fenêtre 7×7 pixels (lissage poussé)\n\nLe MNT lissé sera sauvegardé dans le dossier de sortie",
        ["Aucun", "Faible", "Moyen", "Fort"],
        0,
        False
    )
    
    mnt_final_path = mnt_path
    if ok and smoothing_choice != "Aucun":
        median_size = {'Faible': 3, 'Moyen': 5, 'Fort': 7}[smoothing_choice]
        smoothing_output = os.path.join(output_folder, f"mnt_lisse_{smoothing_choice.lower()}_{to_safe_str(resolution_value)}m.tif")
        median_params = {
            'input': mnt_path,
            'size': median_size,
            'method': 'median',
            'output': smoothing_output
        }
        result = processing.run("grass7:r.neighbors", median_params)
        if 'output' in result and os.path.exists(result['output']):
            mnt_final_path = result['output']
            logging.info(f"MNT lissé sauvegardé : {smoothing_output}")
            QMessageBox.information(
                None,
                "Lissage appliqué",
                f"Le MNT a été lissé avec l'intensité {smoothing_choice} (fenêtre {median_size}×{median_size} pixels).\n" +
                f"Fichier sauvegardé : {os.path.basename(smoothing_output)}"
            )
        else:
            QMessageBox.warning(
                None,
                "Lissage",
                "Le traitement de lissage n'a pas généré de sortie valide. Le MNT original sera utilisé."
            )

    # Option de remplissage des trous léger
    sink_values = ["0.0 m", "0.1 m", "0.2 m", "0.3 m", "0.4 m", "0.5 m", "0.6 m", "0.7 m", "0.8 m", "0.9 m", "1.0 m"]
    sink_filling_choice, ok_sink_filling = QInputDialog.getItem(
        None,
        "Remplissage des trous léger",
        "Sélectionnez la profondeur maximale pour combler les artefacts topographiques (puits artificiels ou bruits isolés).\n"
        "Attention : ce traitement peut également lisser ou supprimer de petites dépressions naturelles.",
        sink_values,
        0,
        False
    )
    
    if ok_sink_filling:
        sink_filling_depth = float(sink_filling_choice.split()[0])
        if sink_filling_depth > 0:
            sink_output = os.path.join(output_folder, f"mnt_corrige_{to_safe_str(resolution_value)}m.tif")
            direction_output = os.path.join(output_folder, "temp_direction.tif")
            stream_output = os.path.join(output_folder, "temp_stream.tif")
            basin_output = os.path.join(output_folder, "temp_basin.tif")
            sink_params = {
                'input': mnt_final_path,
                'threshold': sink_filling_depth,
                'output': sink_output,
                'direction': direction_output,
                'stream': stream_output,
                'basin': basin_output,
                'areas': "TEMPORARY_OUTPUT"
            }
            result_sink = processing.run("grass7:r.fill.dir", sink_params)
            if 'output' in result_sink and os.path.exists(result_sink['output']):
                mnt_final_path = result_sink['output']
                logging.info(f"MNT sans puits sauvegardé : {sink_output}")
                QMessageBox.information(
                    None,
                    "Remplissage des trous appliqué",
                    f"Les petits artefacts ont été comblés jusqu'à {sink_filling_depth} m.\n" +
                    f"Fichier sauvegardé : {os.path.basename(sink_output)}"
                )
                # Nettoyer les fichiers temporaires
                for temp_file in [direction_output, stream_output, basin_output]:
                    if os.path.exists(temp_file):
                        try:
                            os.remove(temp_file)
                        except:
                            pass
            else:
                QMessageBox.warning(
                    None,
                    "Remplissage des trous",
                    "Échec de l'opération. Le MNT original sera utilisé."
                )

    # Seuils de périmètre
    min_perimeter, ok = QInputDialog.getDouble(
        None,
        "Seuil minimum de périmètre",
        "Définit la plus petite taille du périmètre de la dépression prise en compte (en m).",
        5.0,
        0.0,
        10000.0,
        2
    )
    if not ok:
        min_perimeter = 5.0

    max_perimeter, ok = QInputDialog.getDouble(
        None,
        "Seuil maximum de périmètre",
        "Définit la plus grande taille du périmètre de la dépression prise en compte (en m).",
        1000.0,
        0.0,
        10000.0,
        2
    )
    if not ok:
        max_perimeter = 1000.0

    # Seuil de dénivelé minimal
    default_min_diff = resolution_value
    dialog_text = (
        f"Dénivelé minimal requis pour valider une dépression (différence d'altitude entre le point bas et la bordure).\n\n"
        f"📐 Résolution de votre MNT : {resolution_value} m\n\n"
        f"Recommandations :\n"
        f"• Valeur standard : {resolution_value} m (= résolution du MNT)\n"
        f"• Contexte bruité ou micro-reliefs : {resolution_value * 1.5:.1f} - {resolution_value * 2:.1f} m\n"
        f"• Dépressions majeures uniquement : {resolution_value * 3:.1f} - {resolution_value * 5:.1f} m\n\n"
        f"⚠️ Un seuil trop faible génère des faux positifs (artefacts topographiques)\n"
        f"⚠️ Un seuil trop élevé omet les dépressions peu profondes"
    )

    min_diff, ok = QInputDialog.getDouble(
        None,
        "Seuil de dénivelé minimal",
        dialog_text,
        default_min_diff,
        0.1,
        10.0,
        1
    )
    if not ok:
        min_diff = default_min_diff

    logging.info("Seuil de dénivelé minimal sélectionné : %s m", min_diff)
    
        # === DIALOGUE STRATÉGIE ANTI-BUTTES ===
    
    strategy_choice, ok = QInputDialog.getItem(
        None,
        "Stratégie de filtrage anti-buttes",
        "Choisissez la stratégie adaptée à votre contexte topographique :\n\n"
        "🟢 PERMISSIVE (plaine / relief peu marqué)\n"
        "   • Détection exhaustive\n\n"
        "🟡 ÉQUILIBRÉE (plateau / relief marqué)\n"
        "   • Compromis exhaustivité / fiabilité\n\n"
        "🔴 CONSERVATRICE (zone plissée / relief très marqué)\n"
        "   • Filtrage strict + analyse morphométrique renforcée (TEST SUPPLEMENTAIRE)",
        ["🟢 PERMISSIVE (plateau / relief peu marqué)", 
         "🟡 ÉQUILIBRÉE (plateau / relief marqué)", 
         "🔴 CONSERVATRICE (zone plissée / relief très marqué)"],
        1,  # ÉQUILIBRÉE par défaut
        False
    )
    
    if not ok:
        strategy_choice = "🟡 ÉQUILIBRÉE (recommandée)"
    
    # Mapper la stratégie vers les paramètres (TEST4 uniquement, TEST1 corrélation supprimé)
    if "PERMISSIVE" in strategy_choice:
        strategy_name = "PERMISSIVE"
        share_threshold = 0.50
        position_threshold = 0.45
        logging.info("Stratégie PERMISSIVE sélectionnée (détection exhaustive)")
        logging.info("   → Objectif : Maximiser le nombre de détections")
    elif "CONSERVATRICE" in strategy_choice:
        strategy_name = "CONSERVATRICE"
        share_threshold = 0.30
        position_threshold = 0.20
        logging.info("Stratégie CONSERVATRICE sélectionnée (détection sélective)")
        logging.info("   → Objectif : Minimiser les faux positifs")
    else:  # ÉQUILIBRÉE (défaut)
        strategy_name = "ÉQUILIBRÉE"
        share_threshold = 0.40
        position_threshold = 0.30
        logging.info("Stratégie ÉQUILIBRÉE sélectionnée (recommandée)")
        logging.info("   → Objectif : Compromis standard")
    
    logging.info(f"   - Dominance max (TEST2) : {share_threshold}")
    logging.info(f"   - Position max (TEST2) : {position_threshold}")
    
    # Paramètres adaptatifs pour le mode étiquetage
    if is_labeling_mode:
        adaptive_params = get_adaptive_parameters(float(resolution_value), min_diff)
        logging.info(f"Paramètres adaptatifs pour résolution {resolution_value}m :")
        logging.info(f"   - min_diff: {adaptive_params['min_diff']}m")
        logging.info(f"   - buffer_distance: {adaptive_params['buffer_distance']}m")
        logging.info(f"   - min_valid_neighbors: {adaptive_params['min_valid_neighbors']}")
        logging.info(f"   - envelope_validation_ratio: {adaptive_params['envelope_validation_ratio']}")

    return {
        'mnt_path': mnt_final_path,
        'mnt_extent': mnt_extent,
        'min_perimeter': min_perimeter,
        'max_perimeter': max_perimeter,
        'min_diff': min_diff,
        'strategy_name': strategy_name,
        'share_threshold': share_threshold,
        'position_threshold': position_threshold
    }

def get_user_inputs():
    """Collecte tous les paramètres utilisateur nécessaires - Interface refactorisée sans redondance"""
    
    # Sélection de la résolution
    resolution_text = """Sélectionnez la résolution d'analyse :

0.5 m : Très haute résolution
1 m   : Haute résolution 
5 m   : Résolution moyenne
25 m  : Basse résolution 
"""
    
    resolution_choice, ok = QInputDialog.getItem(
        None,
        "Résolution d'analyse",
        resolution_text,
        ["0.5 m", "1 m", "5 m", "25 m"],
        1,
        False
    )
    
    if not ok:
        raise Exception("[ERROR - Étape 0] Aucune résolution sélectionnée.")
    
    # Mapper vers les valeurs internes
    resolution_mapping = {
        "0.5 m": ("0_5", 0.5),
        "1 m": ("1", 1.0),
        "5 m": ("5", 5.0),
        "25 m": ("25", 25.0)
    }
    
    resolution_label, resolution_value = resolution_mapping[resolution_choice]
    
    # Sélection du workflow avec la nouvelle interface
    mode, ok = select_workflow()
    if not ok:
        raise Exception("[ERROR - Étape 0] Aucun workflow sélectionné.")

    # =========================================================================
    # V1.1 : Question BD TOPO - selon le mode de workflow
    # - Mode "DETECTER des dépressions" (sans bassins, sans ML) : pas de BD TOPO
    # - Autres modes : proposition d'activer BD TOPO, puis chargement lazy
    #
    # Note : les noms de modes ont des emojis tels que retournes par
    # select_workflow(). Ils seront traduits en noms internes plus loin.
    # =========================================================================
    use_bdtopo = False

    # Modes qui proposent la question BD TOPO AVANT le choix des fichiers
    # (pour APPLIQUER ML, BD TOPO est decidee APRES selection du modele en
    # lisant son metadata - voir plus bas)
    # V1.2 : "DETECTER + bassins" NE propose PLUS BD TOPO (pas de ML donc
    # features inutiles ; l'utilisateur peut ajouter BD TOPO manuellement).
    modes_with_bdtopo = [
        "🏷️ CRÉER des données d'entraînement ML",
        "🧠 ENTRAÎNER un modèle ML"
    ]

    if mode in modes_with_bdtopo:
        # Message contextuel selon le mode
        if "CRÉER" in mode:
            bdtopo_msg = (
                "Souhaitez-vous utiliser la BD TOPO IGN pour l'etiquetage ?\n\n"
                "La BD TOPO (base de donnees geographique officielle de l'IGN) apporte :\n"
                "  - Affichage des couches bati, routes, hydrographie, zones\n"
                "    d'activite en contexte, pour aider l'etiquetage manuel\n"
                "  - Calcul automatique de 18 features anthropiques ajoutees\n"
                "    aux donnees d'entrainement du modele ML\n\n"
                "Ces features ameliorent la discrimination entre depressions\n"
                "naturelles et anthropiques (bassins, carrieres, etangs artificiels).\n\n"
                "Necessite une connexion internet (WFS IGN Geoplateforme).\n"
                "Premiere utilisation : ~30-60s. Ensuite : cache local."
            )
        elif "ENTRAÎNER" in mode:
            bdtopo_msg = (
                "Vos CSV d'entrainement contiennent-ils les features BD TOPO ?\n\n"
                "Si vous avez etiquete vos donnees avec BD TOPO activee, les CSV\n"
                "contiennent 18 colonnes supplementaires (dist_bati, bati_100m, etc.)\n"
                "que le modele ML exploitera pour une meilleure discrimination\n"
                "des depressions anthropiques.\n\n"
                "Repondez Oui si c'est le cas."
            )
        else:
            # Ne devrait pas arriver vu la nouvelle liste modes_with_bdtopo,
            # mais message generique par securite.
            bdtopo_msg = "Utiliser la BD TOPO IGN pour enrichir l'analyse ML ?"

        reply = QMessageBox.question(
            None, "DEADKAT V1.2 - BD TOPO",
            bdtopo_msg,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            # Chargement LAZY : on tente maintenant de charger les modules
            if ensure_bdtopo_modules_loaded():
                use_bdtopo = True
                logging.info("BD TOPO : activee")
            else:
                use_bdtopo = False
                logging.warning("BD TOPO demandee mais modules indisponibles - continuer sans BD TOPO")
                QMessageBox.warning(
                    None, "BD TOPO indisponible",
                    "Les modules BD TOPO n'ont pas pu etre charges.\n\n"
                    f"Raison : {BDTOPO_ERROR}\n\n"
                    "DEADKAT continuera sans BD TOPO."
                )
        else:
            logging.info("BD TOPO : refusee par l'utilisateur")
    elif "APPLIQUER un modèle ML" in mode:
        # Pour APPLIQUER ML, BD TOPO est decidee APRES selection du modele
        # en fonction du metadata sauvegarde lors de l'entrainement.
        logging.info("BD TOPO : sera determinee apres selection du modele ML")
    else:
        # Mode DETECTER dépressions simples : pas de BD TOPO
        logging.info("BD TOPO : non applicable en mode Depressions seules")

    # INITIALISATION DES VARIABLES
    ml_mode = "disabled"
    ml_model_path = None
    ml_model_folder = None
    naturelles_files = None
    anthropiques_files = None
    faux_files = None
    mnt_path = None
    mnt_extent = None
    output_folder = None
    min_perimeter = None
    max_perimeter = None
    min_diff = None
    threshold = None
    strategy_name = "ÉQUILIBRÉE"
    share_threshold = 0.40
    position_threshold = 0.30
    
    # ENTRAÎNER UN MODÈLE ML
    if "ENTRAÎNER un modèle" in mode:
        ml_mode = "train_new"
        mode = "Entraîner modèle"
        
        if not HAS_SKLEARN:
            QMessageBox.critical(None, "Modules manquants", 
                               "Scikit-learn requis pour l'entraînement ML")
            raise Exception("Scikit-learn requis")
        
        # Sélection des fichiers DÉPRESSIONS NATURELLES
        naturelles_files, _ = QFileDialog.getOpenFileNames(
            None,
            "Sélectionnez vos fichiers DÉPRESSIONS NATURELLES",
            "",
            "Fichiers CSV (*.csv);;Tous les fichiers (*)"
        )
        
        if not naturelles_files:
            raise Exception("Aucun fichier dépressions naturelles sélectionné")
        
        logging.info(f"{len(naturelles_files)} fichier(s) dépressions naturelles sélectionné(s)")
        for f in naturelles_files:
            logging.info(f"   • {os.path.basename(f)}")
        
        # Sélection des fichiers DÉPRESSIONS ANTHROPIQUES
        anthropiques_files, _ = QFileDialog.getOpenFileNames(
            None,
            "Sélectionnez vos fichiers DÉPRESSIONS ANTHROPIQUES",
            "",
            "Fichiers CSV (*.csv);;Tous les fichiers (*)"
        )
        
        if not anthropiques_files:
            QMessageBox.warning(
                None,
                "Aucune dépression anthropique",
                "Vous n'avez sélectionné aucun fichier de dépressions anthropiques.\n"
                "Le modèle sera entraîné uniquement sur 2 classes (naturelles et faux positifs)."
            )
            logging.info("Aucune dépression anthropique fournie - entraînement binaire")
        else:
            logging.info(f"{len(anthropiques_files)} fichier(s) dépressions anthropiques sélectionné(s)")
            for f in anthropiques_files:
                logging.info(f"   • {os.path.basename(f)}")
        
        # Sélection des fichiers FAUX POSITIFS
        faux_files, _ = QFileDialog.getOpenFileNames(
            None,
            "Sélectionnez vos fichiers FAUX POSITIFS",
            "",
            "Fichiers CSV (*.csv);;Tous les fichiers (*)"
        )
        
        if not faux_files:
            raise Exception("Aucun fichier faux positifs sélectionné")
        
        logging.info(f"{len(faux_files)} fichier(s) faux positifs sélectionné(s)")
        for f in faux_files:
            logging.info(f"   • {os.path.basename(f)}")
        
        # Dossier de sortie pour le modèle
        output_folder = QFileDialog.getExistingDirectory(
            None,
            "Dossier de sortie pour le modèle entraîné"
        )
        if not output_folder:
            raise Exception("Dossier de sortie requis")
        
        return {
            'resolution_label': resolution_label,
            'resolution_value': resolution_value,
            'mnt_path': None,
            'output_folder': output_folder,
            'csv_output_folder': None,
            'mnt_extent': None,
            'min_perimeter': None,
            'max_perimeter': None,
            'min_diff': None,
            'threshold': None,
            'mode': mode,
            'ml_mode': ml_mode,
            'ml_model_path': ml_model_path,
            'naturelles_files': naturelles_files,
            'anthropiques_files': anthropiques_files,
            'faux_files': faux_files,
            'strategy_name': 'ÉQUILIBRÉE',           # Valeur par défaut (pas de MNT donc pas de stratégie)
            'share_threshold': 0.40,                 # Valeur par défaut
            'position_threshold': 0.30               # Valeur par défaut
        }
        
    # APPLIQUER UN MODÈLE ML
    elif "APPLIQUER un modèle ML" in mode:
        if "dépressions + bassins" in mode:
            ml_mode = "apply_existing"
            mode = "Appliquer modèle + Bassins"
        else:
            ml_mode = "apply_existing"
            mode = "Appliquer modèle seul"
        
        # Dossier contenant le modèle
        model_folder = QFileDialog.getExistingDirectory(
            None,
            "Sélectionnez le dossier contenant votre modèle ML"
        )
        if not model_folder:
            raise Exception("Dossier de modèle requis")
            
        # Vérifier qu'un modèle existe
        model_path = os.path.join(model_folder, 'modele_rf.joblib')
        if not os.path.exists(model_path):
            model_path = os.path.join(model_folder, 'depression_classifier.joblib')
            if not os.path.exists(model_path):
                QMessageBox.critical(
                    None,
                    "Modèle manquant",
                    f"Aucun modèle trouvé dans :\n{model_folder}\n\n"
                    "Fichiers recherchés :\n"
                    "• modele_rf.joblib\n"
                    "• depression_classifier.joblib\n\n"
                    "Entraînez d'abord un modèle avec 'ENTRAÎNER un modèle ML'."
                )
                raise Exception("Modèle ML manquant")
        
        ml_model_path = model_path
        ml_model_folder = model_folder
        
        # Dossier de sortie pour les résultats
        output_folder = QFileDialog.getExistingDirectory(
            None,
            "Sélectionnez le dossier de sortie pour les résultats"
        )
        if not output_folder:
            raise Exception("Dossier de sortie requis")
        
        logging.info(f"Modèle source : {model_folder}")
        logging.info(f"Résultats dans : {output_folder}")

        # =====================================================================
        # V1.2 : Lecture metadata du modele + dialogue informatif BD TOPO
        # =====================================================================
        import json
        import joblib
        metadata_path = os.path.join(model_folder, 'modele_metadata.json')
        bdtopo_usage = None
        metadata = None

        if os.path.exists(metadata_path):
            try:
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    metadata = json.load(f)
                bdtopo_usage = metadata.get('bdtopo_usage')
                logging.info(f"Metadata modele charge : BD TOPO usage = {bdtopo_usage}")
            except Exception as e:
                logging.warning(f"Erreur lecture metadata : {e}")

        if metadata is None:
            # Modele ancien sans metadata : on deduit des noms de features
            try:
                features_file = os.path.join(model_folder, 'variables.joblib')
                feature_names_tmp = joblib.load(features_file)
                has_bdtopo_feat = any(
                    f in feature_names_tmp or any(f.startswith(c + '_') for c in BDTOPO_CATEGORICAL_SHORT)
                    for f in BDTOPO_NUMERIC_SHORT
                )
                bdtopo_usage = "full" if has_bdtopo_feat else "none"
                logging.info(f"Metadata absente, deduction : BD TOPO usage = {bdtopo_usage}")
            except Exception as e:
                logging.warning(f"Impossible de deduire l'usage BD TOPO : {e}")
                bdtopo_usage = "none"

        # Affichage du dialogue informatif selon le cas
        if bdtopo_usage == "full":
            info_msg = (
                "Ce modele a ete entraine avec la BD TOPO IGN activee.\n\n"
                "Les 18 features anthropiques (batiment, routes, reservoirs,\n"
                "zones d'activite, hydrographie, vegetation, terrains de sport)\n"
                "seront calculees pour les depressions a classifier.\n\n"
                "-> BD TOPO sera telechargee automatiquement\n"
                "   (connexion internet requise)."
            )
            if metadata:
                info_msg += (
                    f"\n\nInfos modele :\n"
                    f"  - {metadata.get('nb_samples_total', '?')} echantillons d'entrainement\n"
                    f"  - Accuracy test : {metadata.get('accuracy_test', 0)*100:.1f} %\n"
                    f"  - Accuracy CV   : {metadata.get('accuracy_cv_mean', 0)*100:.1f} %"
                )
            QMessageBox.information(None, "Modele charge - avec features BD TOPO", info_msg)
            use_bdtopo = True

        elif bdtopo_usage == "partial":
            info_msg = (
                "Ce modele a ete entraine sur un corpus mixte.\n\n"
                f"- {metadata.get('nb_samples_with_bdtopo', '?')} echantillons sur "
                f"{metadata.get('nb_samples_total', '?')} disposaient des features BD TOPO.\n\n"
                "Le modele peut fonctionner avec ou sans BD TOPO, mais ses\n"
                "performances seront meilleures si BD TOPO est activee.\n\n"
                "-> BD TOPO sera telechargee automatiquement pour garantir\n"
                "   la coherence avec l'entrainement."
            )
            if metadata:
                info_msg += (
                    f"\n\nInfos modele :\n"
                    f"  - Accuracy test : {metadata.get('accuracy_test', 0)*100:.1f} %\n"
                    f"  - Accuracy CV   : {metadata.get('accuracy_cv_mean', 0)*100:.1f} %"
                )
            QMessageBox.information(None, "Modele charge - features BD TOPO partielles", info_msg)
            use_bdtopo = True

        else:  # "none" ou inconnu
            info_msg = (
                "Ce modele a ete entraine uniquement sur les features\n"
                "geomorphometriques :\n"
                "  Perimeter, Area, Circularity, Depth,\n"
                "  AltMin, AltMax, Num_Polygons\n\n"
                "-> Pas de telechargement BD TOPO, inference directe."
            )
            if metadata:
                info_msg += (
                    f"\n\nInfos modele :\n"
                    f"  - {metadata.get('nb_samples_total', '?')} echantillons d'entrainement\n"
                    f"  - Accuracy test : {metadata.get('accuracy_test', 0)*100:.1f} %\n"
                    f"  - Accuracy CV   : {metadata.get('accuracy_cv_mean', 0)*100:.1f} %"
                )
            QMessageBox.information(None, "Modele charge - sans BD TOPO", info_msg)
            use_bdtopo = False

        # Chargement effectif des modules BD TOPO si necessaire
        if use_bdtopo:
            if ensure_bdtopo_modules_loaded():
                logging.info("BD TOPO : activee pour l'inference")
            else:
                QMessageBox.warning(
                    None, "BD TOPO indisponible",
                    "Les modules BD TOPO sont requis mais introuvables.\n\n"
                    f"Raison : {BDTOPO_ERROR}\n\n"
                    "L'inference peut continuer mais les predictions seront incorrectes."
                )
                use_bdtopo = False
        
    # MODES CLASSIQUES
    elif "DÉTECTER des dépressions" in mode:
        ml_mode = "disabled"
        if "bassins associés" in mode:
            mode = "Dépressions + Bassins classique"
        else:
            mode = "Dépressions seules"
        
        # Dossier de sortie
        output_folder = QFileDialog.getExistingDirectory(
            None,
            "Sélectionnez le dossier de sortie"
        )
        if not output_folder:
            raise Exception("[ERROR] Dossier de sortie requis.")
            
    # CRÉER DES DONNÉES D'ENTRAÎNEMENT ML
    elif "CRÉER des données" in mode:
        ml_mode = "labeling"
        mode = "Interface étiquetage"
        
        # Dossier de sortie unique (SHP + CSV)
        output_folder = QFileDialog.getExistingDirectory(
            None,
            "Dossier de sortie (shapefiles + fichiers training)"
        )
        if not output_folder:
            raise Exception("[ERROR] Dossier de sortie requis")
        logging.info(f"Dossier sortie : {output_folder}")
        
        os.makedirs(output_folder, exist_ok=True)

    # SÉLECTION MNT ET PARAMÈTRES COMMUNS
    if ml_mode != "train_new":
        # Sélection du MNT
        mnt_path, _ = QFileDialog.getOpenFileName(
            None,
            "Sélectionnez le fichier MNT",
            "",
            "Fichiers TIFF (*.tif *.tiff);;Tous les fichiers (*)"
        )
        
        if not mnt_path:
            raise Exception("[ERROR] Fichier MNT requis.")
        
        logging.info(f"MNT sélectionné : {mnt_path}")
        
        # Traitement MNT et paramètres - UNE SEULE FOIS
        mnt_params = get_mnt_processing_parameters(mnt_path, output_folder, resolution_value)
        
        mnt_path = mnt_params['mnt_path']
        mnt_extent = mnt_params['mnt_extent']
        min_perimeter = mnt_params['min_perimeter']
        max_perimeter = mnt_params['max_perimeter']
        min_diff = mnt_params['min_diff']
        strategy_name = mnt_params['strategy_name']
        share_threshold = mnt_params['share_threshold']
        position_threshold = mnt_params['position_threshold']
        
        # Seuil de drainage (si bassins d'alimentation)
        if "bassins" in mode.lower() or "Bassins" in mode:
            threshold, ok = QInputDialog.getInt(
                None,
                "Seuil de drainage",
                "Fixe la taille minimale d'un bassin d'alimentation en pixels MNT. Un seuil de 10000 correspond à un bassin de 10000 m² pour une résolution de 1 m.",
                10000,
                1,
                1000000
            )
            if not ok:
                threshold = 10000

    # RETOUR FINAL
    logging.info(f"Paramètres collectés:")
    logging.info(f"   - Mode: {mode}")
    logging.info(f"   - ML Mode: {ml_mode}")
    logging.info(f"   - Résolution: {resolution_value}m")
    if mnt_path:
        logging.info(f"   - MNT: {mnt_path}")
    logging.info(f"   - Sortie: {output_folder}")

    return {
        'resolution_label': resolution_label,
        'resolution_value': resolution_value,
        'mnt_path': mnt_path,
        'output_folder': output_folder,
        'csv_output_folder': output_folder,
        'mnt_extent': mnt_extent,
        'min_perimeter': min_perimeter,
        'max_perimeter': max_perimeter,
        'min_diff': min_diff,
        'threshold': threshold,
        'mode': mode,
        'ml_mode': ml_mode,
        'ml_model_path': ml_model_path,
        'ml_model_folder': ml_model_folder,
        'naturelles_files': naturelles_files,
        'anthropiques_files': anthropiques_files,
        'faux_files': faux_files,
        'strategy_name': mnt_params.get('strategy_name', 'ÉQUILIBRÉE'),
        'share_threshold': mnt_params.get('share_threshold', 0.40),
        'position_threshold': mnt_params.get('position_threshold', 0.30),
        'use_bdtopo': use_bdtopo  # V1.1
    }


# =============================================================================
# 8. ÉTAPES 1-7 : DÉTECTION DES DÉPRESSIONS
# =============================================================================

def step1_extract_contours(mnt_path, output_folder, resolution):
    """Étape 1 : Extraction des courbes de niveau - échelle unique"""
    logging.info("Étape 1 : Extraction des courbes de niveau")
    
    interval = resolution  # Utiliser directement la résolution
    safe_interval = to_safe_str(interval)
    output_path = os.path.join(output_folder, f'courbes_niveau_{to_safe_str(resolution)}m.gpkg')
    
    if os.path.exists(output_path):
        logging.info("Le fichier %s existe déjà.", output_path)
        return interval
    
    try:
        processing.run("gdal:contour", {
            'INPUT': mnt_path,
            'BAND': 1,
            'INTERVAL': interval,
            'FIELD_NAME': 'ELEV',
            'OUTPUT': output_path
        })
        logging.info("Courbes de niveau %s m générées : %s", interval, output_path)
    except Exception as e:
        logging.error("Échec de la génération des courbes de niveau %s m : %s", interval, e)
        raise
    
    logging.info("Étape 1 validée.")
    return interval

def step2_polygonize_contours(output_folder, resolution, interval):
    """Étape 2 : Conversion des courbes de niveau en polygones"""
    logging.info("Étape 2 : Conversion des courbes de niveau en polygones")
    
    interval = resolution
    safe_interval = to_safe_str(interval)
    contours_output = os.path.join(output_folder, f'courbes_niveau_{to_safe_str(resolution)}m.gpkg')
    polygons_output = os.path.join(output_folder, f'polygones_bruts_{to_safe_str(resolution)}m.gpkg')
    
    if not os.path.exists(contours_output):
        raise Exception(f"❌ Fichier d'entrée manquant: {contours_output}")
        
    if os.path.exists(polygons_output):
        logging.info("Le fichier %s existe déjà.", polygons_output)
        return
    
    try:
        processing.run("qgis:polygonize", {
            'INPUT': contours_output,
            'OUTPUT': polygons_output
        })
        if os.path.exists(polygons_output):
            logging.info("Polygones %s m générés : %s", interval, polygons_output)
        else:
            raise Exception("Fichier non généré.")
    except Exception as e:
        logging.error("Échec de la conversion en polygones : %s", e)
        raise
    
    logging.info("Étape 2 validée.")

def add_fields_and_calculate_attributes(layer, contours_layer, tolerance=50, neighbor_count=5):
    """Ajoute les champs et calcule les attributs - Version optimisée"""
    provider = layer.dataProvider()
    
    # Vérification et ajout des champs
    existing = set(f.name() for f in layer.fields())
    new_fields = []
    for name in ('Perimeter', 'Area', 'Altitude'):
        if name not in existing:
            new_fields.append(QgsField(name, QVariant.Double))
    if new_fields:
        provider.addAttributes(new_fields)
        layer.updateFields()

    # Préparer contours : liste, dict, index spatial
    contour_feats = list(contours_layer.getFeatures())
    contour_dict = {feat.id(): feat for feat in contour_feats}
    spatial_index = QgsSpatialIndex()
    for f in contour_feats:
        spatial_index.insertFeature(f)
    
    centroids = {fid: contour_dict[fid].geometry().centroid().asPoint() for fid in contour_dict}

    # Index des champs
    idx_perim = layer.fields().lookupField('Perimeter')
    idx_area = layer.fields().lookupField('Area')
    idx_alt = layer.fields().lookupField('Altitude')

    updates = {}
    if HAS_NUMPY:
        hypot = np.hypot
    else:
        hypot = lambda dx, dy: math.sqrt(dx*dx + dy*dy)
    
    get = contour_dict.get
    
    for feat in layer.getFeatures():
        fid = feat.id()
        geom = feat.geometry()
        if not geom or geom.isEmpty():
            logging.warning('Feature %d géométrie invalide', fid)
            continue
        
        perim = geom.length()
        area = geom.area()
        center = geom.centroid().asPoint()
        neigh_ids = spatial_index.nearestNeighbor(center, neighbor_count)
        
        altitude = None
        if HAS_NUMPY and neigh_ids:
            pts = np.array([[centroids[n].x(), centroids[n].y()] for n in neigh_ids if n in centroids])
            if pts.size:
                dx = pts[:,0] - center.x()
                dy = pts[:,1] - center.y()
                dists = hypot(dx, dy)
                mi = int(dists.argmin())
                if dists[mi] <= tolerance:
                    nearest_id = neigh_ids[mi]
                    cf = get(nearest_id)
                    if cf:
                        altitude = cf['ELEV']
        else:
            # Version sans numpy
            min_dist = float('inf')
            nearest_id = None
            for n in neigh_ids:
                if n in centroids:
                    pt = centroids[n]
                    dx = pt.x() - center.x()
                    dy = pt.y() - center.y()
                    dist = math.sqrt(dx*dx + dy*dy)
                    if dist < min_dist:
                        min_dist = dist
                        nearest_id = n
            if min_dist <= tolerance and nearest_id:
                cf = get(nearest_id)
                if cf:
                    altitude = cf['ELEV']
        
        if altitude is None:
            logging.warning('Aucune altitude pour feature %d', fid)
        
        updates[fid] = {idx_perim: perim, idx_area: area, idx_alt: altitude}

    provider.changeAttributeValues(updates)
    logging.info('Attributs calculés pour couche %s (%d entités)', layer.name(), len(updates))


def step3_add_fields_and_attributes(output_folder, resolution, tolerance=50, neighbor_count=5):
    """Étape 3 : Ajout des champs et calcul des attributs"""
    logging.info('Étape 3 : Ajout des champs et calcul des attributs')
    
    interval = resolution
    safe_interval = to_safe_str(interval)
    contours_path = os.path.join(output_folder, f'courbes_niveau_{to_safe_str(resolution)}m.gpkg')
    poly_path = os.path.join(output_folder, f'polygones_bruts_{to_safe_str(resolution)}m.gpkg')
    
    if not os.path.exists(contours_path):
        raise Exception(f'❌ Fichier manquant: {contours_path}')
    if not os.path.exists(poly_path):
        raise Exception(f'❌ Fichier manquant: {poly_path}')
        
    contours_lyr = QgsVectorLayer(contours_path, f'contours_{interval}m', 'ogr')
    poly_lyr = QgsVectorLayer(poly_path, f'polygons_{interval}m', 'ogr')
    
    if not contours_lyr.isValid() or not poly_lyr.isValid():
        raise Exception(f'❌ Couches invalides')
        
    if contours_lyr.featureCount() == 0 or poly_lyr.featureCount() == 0:
        raise Exception(f'❌ Couches vides')
        
    try:
        add_fields_and_calculate_attributes(poly_lyr, contours_lyr, tolerance, neighbor_count)
        out_path = os.path.join(output_folder, f'polygones_attributs_{to_safe_str(resolution)}m.gpkg')
        QgsVectorFileWriter.writeAsVectorFormat(poly_lyr, out_path, 'utf-8', poly_lyr.crs(), 'GPKG')
        logging.info('✅ Couche enregistrée : %s', out_path)
    except Exception as e:
        logging.error('❌ Erreur étape 3 : %s', e)
        raise
    
    logging.info('✅ Étape 3 validée.')

def delete_shapefile_safely(shapefile_path):
    """Supprime un fichier vecteur (GPKG ou ancien SHP avec ses fichiers associés).
    Note : le nom est garde pour compatibilite avec l'existant mais gere les 2 formats."""
    # Cas GPKG : un seul fichier
    if shapefile_path.endswith('.gpkg'):
        if os.path.exists(shapefile_path):
            try:
                os.remove(shapefile_path)
                logging.info(f"🗑️ Fichier supprimé: {os.path.basename(shapefile_path)}")
            except Exception as e:
                logging.warning(f"⚠️ Impossible de supprimer {shapefile_path}: {e}")
        return

    # Cas SHP (legacy) : supprimer tous les fichiers compagnons
    if not shapefile_path.endswith('.shp'):
        shapefile_path += '.shp'

    base_path = shapefile_path.replace('.shp', '')
    extensions = ['.shp', '.shx', '.dbf', '.prj', '.cpg', '.qpj', '.sbn', '.sbx', '.shp.xml']

    deleted = []
    for ext in extensions:
        file_path = base_path + ext
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                deleted.append(ext)
            except Exception as e:
                logging.warning(f"⚠️ Impossible de supprimer {file_path}: {e}")

    if deleted:
        logging.info(f"🗑️ Fichiers supprimés: {', '.join(deleted)}")


def step4_filter_polygons(output_folder, resolution_value, min_perimeter, max_perimeter):
    """Étape 4 : Filtrage des polygones par périmètre - VERSION NETTOYÉE"""
    logging.info("Étape 4 : Filtrage des polygones par périmètre")
    
    interval = resolution_value
    safe_interval = to_safe_str(interval)
    
    polygons_layer_path = os.path.join(
        output_folder, 
        f'polygones_attributs_{to_safe_str(resolution_value)}m.gpkg'
    )
    filtered_layer_path = os.path.join(
        output_folder, 
        f'polygones_filtres_{to_safe_str(resolution_value)}m.gpkg'
    )
    
    # NETTOYER le shapefile de sortie s'il existe déjà
    if os.path.exists(filtered_layer_path):
        logging.info(f"🗑️ Suppression de l'ancien filtered_layer...")
        delete_shapefile_safely(filtered_layer_path)
    
    # Vérifications préliminaires
    if not os.path.exists(polygons_layer_path):
        raise Exception(f"❌ Fichier manquant: {polygons_layer_path}")
    
    polygons_layer = QgsVectorLayer(polygons_layer_path, f"polygones_bruts_{to_safe_str(resolution_value)}m", "ogr")
    
    if not polygons_layer.isValid():
        raise Exception(f"❌ Couche source invalide: {polygons_layer_path}")
    
    if polygons_layer.featureCount() == 0:
        raise Exception(f"❌ Couche source vide: {polygons_layer_path}")
    
    # Filtrage par périmètre
    expr = f'"Perimeter" > {min_perimeter} AND "Perimeter" <= {max_perimeter}'
    request = QgsFeatureRequest().setFilterExpression(expr)
    filtered_features = [feat for feat in polygons_layer.getFeatures(request)]
    
    logging.info(f"Filtrage: {len(filtered_features)} features sur {polygons_layer.featureCount()}")
    
    if len(filtered_features) == 0:
        raise Exception(f"❌ Aucune feature après filtrage (périmètre {min_perimeter}-{max_perimeter}m)")
    
    # Créer la couche filtrée EN MÉMOIRE
    filtered_layer = QgsVectorLayer(
        f"Polygon?crs={polygons_layer.crs().authid()}", 
        f"polygones_filtres_{to_safe_str(resolution_value)}m", 
        "memory"
    )
    
    filtered_provider = filtered_layer.dataProvider()
    filtered_provider.addAttributes(polygons_layer.fields())
    filtered_layer.updateFields()
    filtered_provider.addFeatures(filtered_features)
    filtered_layer.updateExtents()
    
    # VÉRIFICATION que la couche mémoire est valide
    if not filtered_layer.isValid():
        raise Exception("❌ Couche mémoire filtrée invalide")
    
    if filtered_layer.featureCount() != len(filtered_features):
        raise Exception(f"❌ Perte de features: {len(filtered_features)} → {filtered_layer.featureCount()}")
    
    # Sauvegarde avec options explicites
    save_options = QgsVectorFileWriter.SaveVectorOptions()
    save_options.driverName = "GPKG"
    save_options.fileEncoding = "UTF-8"
    
    write_result = QgsVectorFileWriter.writeAsVectorFormatV3(
        filtered_layer,
        filtered_layer_path,
        QgsCoordinateTransformContext(),
        save_options
    )
    
    if write_result[0] != QgsVectorFileWriter.NoError:
        raise Exception(f"❌ Erreur écriture shapefile: {write_result[1]}")
    
    # Vérifier que le fichier existe physiquement
    if not os.path.exists(filtered_layer_path):
        raise Exception(f"❌ Fichier non créé sur disque: {filtered_layer_path}")
    
    # Rechargement depuis le disque
    final_layer = QgsVectorLayer(filtered_layer_path, f"filtered_layer_{interval}m", "ogr")
    
    if not final_layer.isValid():
        logging.error(f"Chemin: {filtered_layer_path}")
        logging.error(f"Existe: {os.path.exists(filtered_layer_path)}")
        logging.error(f"Taille: {os.path.getsize(filtered_layer_path) if os.path.exists(filtered_layer_path) else 0}")
        raise Exception(f"❌ Impossible de recharger la couche filtrée depuis: {filtered_layer_path}")
    
    if final_layer.featureCount() == 0:
        raise Exception(f"❌ Couche rechargée vide (était: {len(filtered_features)} features)")
    
    logging.info(f"✅ Étape 4 validée: {final_layer.featureCount()} dépressions filtrées")
    return final_layer

def step5_identify_envelopes(output_folder, resolution):
    """Étape 5 : Identification des enveloppes"""
    logging.info("Étape 5 : Identification des enveloppes")
    
    interval = resolution
    safe_interval = to_safe_str(interval)
    filtered_layer_path = os.path.join(output_folder, f'polygones_filtres_{to_safe_str(resolution)}m.gpkg')
    selected_envelopes_output = os.path.join(output_folder, f'enveloppes_{to_safe_str(resolution)}m.gpkg')
    
    if os.path.exists(selected_envelopes_output):
        logging.info(f"✅ Le fichier {selected_envelopes_output} existe déjà.")
        return selected_envelopes_output
    
    if not os.path.exists(filtered_layer_path):
        raise Exception(f"[ERREUR] Fichier manquant: {filtered_layer_path}")
    
    filtered_layer = QgsVectorLayer(filtered_layer_path, f"filtered_layer_{interval}m", "ogr")
    if not filtered_layer.isValid():
        raise Exception(f"[ERREUR] Couche filtered_layer_{interval}m invalide")
    
    if filtered_layer.featureCount() == 0:
        raise Exception(f"[ERREUR] Couche filtered_layer_{interval}m vide")
    
    provider = filtered_layer.dataProvider()
    if "Numero" not in [f.name() for f in provider.fields()]:
        provider.addAttributes([QgsField("Numero", QVariant.String)])
        filtered_layer.updateFields()
    
    spatial_index = QgsSpatialIndex(filtered_layer.getFeatures())
    envelope_index = 1
    visited = set()
    connected_groups = {}
    
    for feature in filtered_layer.getFeatures():
        fid = feature.id()
        if fid in visited:
            continue
        
        to_visit = [fid]
        connected_features = []
        
        while to_visit:
            current_id = to_visit.pop()
            if current_id in visited:
                continue
            
            visited.add(current_id)
            current_feature = filtered_layer.getFeature(current_id)
            connected_features.append(current_feature)
            
            intersecting_ids = spatial_index.intersects(current_feature.geometry().boundingBox())
            for intersecting_id in intersecting_ids:
                if intersecting_id not in visited:
                    neighbor = filtered_layer.getFeature(intersecting_id)
                    if neighbor.geometry().intersects(current_feature.geometry()):
                        to_visit.append(intersecting_id)
        
        if len(connected_features) >= 2:
            connected_groups[envelope_index] = connected_features
            envelope_index += 1
    
    # Mise à jour des attributs
    with edit(filtered_layer):
        for env_num, feats in connected_groups.items():
            for f in feats:
                f.setAttribute(f.fieldNameIndex("Numero"), f"Envelop{env_num}")
                filtered_layer.updateFeature(f)
    
    # Supprimer les features isolées (non groupées)
    ids_to_delete = [f.id() for f in filtered_layer.getFeatures() if not str(f["Numero"]).startswith("Envelop")]
    if ids_to_delete:
        with edit(filtered_layer):
            for fid in ids_to_delete:
                filtered_layer.deleteFeature(fid)
    
    # Sauvegarder
    QgsVectorFileWriter.writeAsVectorFormat(filtered_layer, selected_envelopes_output, "utf-8", filtered_layer.crs(), "GPKG")
    
    num_envelopes = len(connected_groups)
    if num_envelopes == 0:
        raise Exception("❌ Aucune enveloppe identifiée pour continuer l'analyse.")
    
    logging.info(f"✅ Nombre total d'enveloppes identifiées : {num_envelopes}")
    logging.info("✅ Étape 5 validée.")
    return selected_envelopes_output

def step6_identify_depressions(output_folder, resolution, filtered_layer, min_diff, 
                               share_threshold=0.40, position_threshold=0.30,
                               strategy_name="ÉQUILIBRÉE"):
    """
    Étape 6 : Identification des formes fermées avec filtrage anti-butte organisé
    
    VERSION AVEC TEST6 INTÉGRÉ (Complexité morphologique excessive)
    
    Args:
        output_folder: Dossier de sortie
        resolution: Résolution du MNT
        filtered_layer: Couche des polygones filtrés
        min_diff: Dénivelé minimal requis
        share_threshold: Dominance surfacique maximale (défaut: 0.40)
        position_threshold: Position altimétrique maximale (défaut: 0.30)
        strategy_name: Stratégie anti-butte ("PERMISSIVE", "ÉQUILIBRÉE", "CONSERVATRICE")
    
    Tests appliqués dans l'ordre :
    - TEST1 : Butte (petit sommet, grande base)
    - TEST2 : Dominance en bas
    - TEST3 : Butte extrême (petit sommet, grosse base)
    - TEST4 : Amplitude altitudinale excessive
    - TEST5 : Tests micro-dépressions (médianes, tiers, progression)
    - TEST6 : Complexité morphologique excessive (CONSERVATRICE uniquement)
    """
    import math
    import os
    from collections import defaultdict
    
    logging.info("Étape 6 : Identification des formes fermées avec tests anti-butte")
    logging.info(f"Paramètres stratégie anti-buttes :")
    logging.info(f"   - Dominance max : {share_threshold}")
    logging.info(f"   - Position max : {position_threshold}")
    
    # Chemin de sortie
    out_name = f"depressions_verifiees_{to_safe_str(resolution)}m.gpkg"
    depressions_output = os.path.join(output_folder, out_name)

    # Vérifier si déjà traité
    if os.path.exists(depressions_output):
        logging.info(f"Le fichier {depressions_output} existe déjà.")
        return depressions_output

    # Charger la couche d'enveloppes (step5)
    if isinstance(filtered_layer, str):
        envelopes_path = filtered_layer
    else:
        safe_res = to_safe_str(resolution)
        envelopes_path = os.path.join(output_folder, f'enveloppes_{safe_res}m.gpkg')
    
    if not os.path.exists(envelopes_path):
        raise Exception(f"Fichier enveloppes manquant: {envelopes_path}")
    
    envelopes_layer = QgsVectorLayer(envelopes_path, "selected_envelopes", "ogr")
    if not envelopes_layer.isValid() or envelopes_layer.featureCount() == 0:
        raise Exception("Couche des enveloppes invalide ou vide")

    # Diagnostic des champs
    field_names = [f.name() for f in envelopes_layer.fields()]
    logging.info(f"Champs disponibles: {field_names}")
    
    if "Numero" not in field_names:
        raise Exception("Champ 'Numero' manquant dans la couche des enveloppes")

    # Regrouper par numéro d'enveloppe
    envelope_groups = defaultdict(list)
    for feature in envelopes_layer.getFeatures():
        numero = safe_get_attribute(feature, "Numero", None)
        if numero is not None and str(numero) != "None":
            envelope_groups[numero].append(feature)

    logging.info(f"Enveloppes à analyser: {len(envelope_groups)}")

    # Statistiques de filtrage détaillées (AVEC TEST6)
    stats = {
        "total": len(envelope_groups),
        "accepted": 0,
        "rejected_denivele": 0,
        "rejected_single": 0,
        "rejected_altitude": 0,
        "rejected_TEST1": 0,
        "rejected_TEST2": 0,
        "rejected_TEST3": 0,
        "rejected_TEST4": 0,
        "rejected_TEST5": 0,
        "rejected_TEST6": 0  # ← NOUVEAU
    }

    accepted_features = []
    rejected_features = []
    rejected_reasons = {}

    # Analyser chaque enveloppe
    for env_num, feats in envelope_groups.items():
        rejection_reason = None
        
        # Pré-requis : Au moins 2 polygones
        if len(feats) < 2:
            stats["rejected_single"] += 1
            rejection_reason = "Polygone unique"
            rejected_features.extend(feats)
            rejected_reasons[env_num] = rejection_reason
            continue

        # Extraire les altitudes avec gestion des virgules décimales
        alts = []
        areas = []
        valid_features = []
        
        for f in feats:
            alt_raw = safe_get_attribute(f, "Altitude", None)
            if alt_raw is not None:
                try:
                    if isinstance(alt_raw, str) and ',' in str(alt_raw):
                        alt = float(str(alt_raw).replace(',', '.'))
                    else:
                        alt = float(alt_raw)
                    
                    alts.append(alt)
                    areas.append(f.geometry().area())
                    valid_features.append(f)
                except (ValueError, TypeError):
                    continue

        if len(alts) < 2:
            stats["rejected_altitude"] += 1
            rejection_reason = "Altitude manquante"
            rejected_features.extend(feats)
            rejected_reasons[env_num] = rejection_reason
            continue

        # Calcul du dénivelé
        alt_min = min(alts)
        alt_max = max(alts)
        alt_range = alt_max - alt_min

        # Pré-requis : Dénivelé minimum
        if alt_range < min_diff:
            stats["rejected_denivele"] += 1
            rejection_reason = f"Dénivelé insuffisant ({alt_range:.2f}m < {min_diff}m)"
            rejected_features.extend(valid_features)
            rejected_reasons[env_num] = rejection_reason
            continue

        # Préparation pour les tests anti-butte
        n = len(alts)
        is_butte = False

        # Tri par altitude
        sorted_indices = sorted(range(n), key=lambda i: alts[i])
        alts_sorted = [alts[i] for i in sorted_indices]
        areas_sorted = [areas[i] for i in sorted_indices]

        # Variables communes pour les tests
        total_area = sum(areas_sorted)
        largest_idx = areas_sorted.index(max(areas_sorted))
        smallest_idx = areas_sorted.index(min(areas_sorted))
        largest_area = areas_sorted[largest_idx]
        smallest_area = areas_sorted[smallest_idx]
        largest_pos = ((alts_sorted[largest_idx] - alt_min) / alt_range) if alt_range else 0.0
        smallest_pos = ((alts_sorted[smallest_idx] - alt_min) / alt_range) if alt_range else 0.0
        largest_share = largest_area / total_area
        ratio_max_min = largest_area / (smallest_area + 1e-6)

        # ===== TEST1 : Butte (petit sommet, grande base) =====
        if not is_butte and n >= 3:
            # Surface cumulée au sommet (altitude max exacte)
            surface_at_top = sum(
                areas_sorted[i] for i in range(n) 
                if alts_sorted[i] == alt_max
            )
            
            # Surface cumulée à la base (altitude min exacte)
            surface_at_bottom = sum(
                areas_sorted[i] for i in range(n) 
                if alts_sorted[i] == alt_min
            )
            
            # Butte = base beaucoup plus grande que sommet
            if surface_at_top > 0 and surface_at_bottom > 0:
                ratio_base_sommet = surface_at_bottom / surface_at_top
                
                # BUTTE : base >> sommet (ratio > 20)
                if ratio_base_sommet > 20:
                    is_butte = True
                    rejection_reason = f"TEST1 - Butte détectée (base/sommet = {ratio_base_sommet:.1f})"
                    stats["rejected_TEST1"] += 1

        # ===== TEST2 : Dominance en bas =====
        if not is_butte:
            if largest_share > share_threshold and largest_pos < position_threshold:
                is_butte = True
                rejection_reason = f"TEST2 - Dominance en bas (share={largest_share:.2f}, pos={largest_pos:.2f})"
                stats["rejected_TEST2"] += 1

        # ===== TEST3 : Butte extrême =====
        if not is_butte and n >= 3:
            if smallest_pos > 0.75 and largest_pos < 0.35 and ratio_max_min > 50:
                is_butte = True
                rejection_reason = "TEST3 - Butte extrême"
                stats["rejected_TEST3"] += 1

        # ===== TEST4 : Amplitude altitudinale excessive =====
        if not is_butte:
            max_depth_threshold = max(15.0, resolution * 5)
            if alt_range > max_depth_threshold:
                is_butte = True
                rejection_reason = f"TEST4 - Amplitude excessive ({alt_range:.1f}m > {max_depth_threshold:.1f}m)"
                stats["rejected_TEST4"] += 1

        # ═════════════════════════════════════════════════════════════════
        # TEST6 : CORRÉLATION ALTITUDE-SURFACE (Distribution altimétrique)
        # (CONSERVATRICE uniquement - Zone plissée)
        # ═════════════════════════════════════════════════════════════════
        if not is_butte and strategy_name == "CONSERVATRICE":
            
            # Import scipy pour corrélation de Pearson
            from scipy.stats import pearsonr
            
            
            # Calculer corrélation de Pearson
            try:
                correlation, p_value = pearsonr(alts, areas)
            except:
                # En cas d'erreur (données invalides), valeur neutre
                correlation = 0.0
            
            SEUIL_CORRELATION = 0.00  # Corrélation nulle = limite
                     
            # Test : corrélation négative → butte suspectée
            if correlation < SEUIL_CORRELATION:
                is_butte = True
                rejection_reason = (
                    f"TEST6 - Distribution altimétrique de butte "
                    f"(corr={correlation:.3f})"
                )
                stats["rejected_TEST6"] += 1
                
                # Logging détaillé pour analyse
                logging.debug(
                    f"TEST6 rejet {env_num}: "
                    f"correlation={correlation:.4f} (< {SEUIL_CORRELATION}), "
                    f"n={n}, total_area={total_area:.1f}, alt_range={alt_range:.2f}"
                )

        # ===== TEST5 : Tests micro-dépressions =====
        if not is_butte and alt_range <= 1.5:
            test5_failed = False
            
            # 5a) Test médianes
            if n >= 5 and HAS_NUMPY:
                mid = n // 2
                median_bottom = np.median(areas_sorted[:mid + 1])
                median_top = np.median(areas_sorted[mid:])
                if median_top > 0:
                    median_ratio = median_bottom / median_top
                    if median_ratio > 10.0:
                        test5_failed = True

            # 5b) Test tiers
            if not test5_failed and n >= 3 and HAS_NUMPY:
                tier = max(1, n // 3)
                avg_bottom = np.mean(areas_sorted[:tier])
                avg_top = np.mean(areas_sorted[-tier:])
                avg_ratio = avg_bottom / (avg_top + 1e-6)
                if avg_ratio > 3.0:
                    test5_failed = True

            # 5c) Test progression décroissante
            if not test5_failed and n >= 4:
                dec_count = sum(1 for i in range(1, n) if areas_sorted[i] < areas_sorted[i - 1] * 0.9)
                dec_ratio = dec_count / n
                if dec_ratio >= 0.7:
                    test5_failed = True

            if test5_failed:
                is_butte = True
                rejection_reason = "TEST5 - Micro-dépressions"
                stats["rejected_TEST5"] += 1

        # Décision finale
        if not is_butte:
            accepted_features.extend(valid_features)
            stats["accepted"] += 1
        else:
            rejected_features.extend(valid_features)
            rejected_reasons[env_num] = rejection_reason

    # Vérifier qu'il y a des résultats
    if not accepted_features:
        logging.error("DÉTAIL DES REJETS:")
        for key, value in stats.items():
            if key.startswith("rejected_") and value > 0:
                logging.error(f"  {key}: {value}")
        raise Exception(f"Aucune forme fermée identifiée avec min_diff={min_diff} m")

    # === CRÉER LA COUCHE DES FORMES ACCEPTÉES ===
    depressions_layer = QgsVectorLayer(
        "Polygon?crs=" + envelopes_layer.crs().authid(),
        f"depressions_verifiees_{to_safe_str(resolution)}m",
        "memory"
    )
    
    provider = depressions_layer.dataProvider()
    provider.addAttributes(envelopes_layer.fields())
    depressions_layer.updateFields()

    # Vérifier que le champ Numero est bien présent
    output_fields = [f.name() for f in depressions_layer.fields()]
    if "Numero" not in output_fields:
        raise Exception("Erreur: champ Numero perdu lors de la création de la couche")

    provider.addFeatures(accepted_features)
    depressions_layer.updateExtents()

    # Sauvegarder sur disque
    write_result = QgsVectorFileWriter.writeAsVectorFormat(
        depressions_layer,
        depressions_output,
        "utf-8",
        depressions_layer.crs(),
        "GPKG"
    )
    
    if write_result[0] != QgsVectorFileWriter.NoError:
        raise Exception(f"Erreur sauvegarde: {write_result[1]}")

    # === SAUVEGARDER LES FORMES REJETÉES ===
    if rejected_features:
        rejected_output = os.path.join(output_folder, f"formes_rejetees_{to_safe_str(resolution)}m.gpkg")
        
        rejected_layer = QgsVectorLayer(
            "Polygon?crs=" + envelopes_layer.crs().authid(),
            "formes_rejetees",
            "memory"
        )
        
        rej_provider = rejected_layer.dataProvider()
        rej_provider.addAttributes(envelopes_layer.fields())
        
        # Ajouter un champ pour la raison du rejet
        rej_provider.addAttributes([QgsField("Raison_Rej", QVariant.String)])
        rejected_layer.updateFields()
        
        # Ajouter les features avec la raison du rejet
        for feat in rejected_features:
            new_feat = QgsFeature(rejected_layer.fields())
            new_feat.setGeometry(feat.geometry())
            
            # Copier les attributs existants
            for i, field in enumerate(envelopes_layer.fields()):
                new_feat[field.name()] = feat[field.name()]
            
            # Ajouter la raison du rejet
            env_num = safe_get_attribute(feat, "Numero", None)
            new_feat["Raison_Rej"] = rejected_reasons.get(env_num, "Inconnu")
            
            rej_provider.addFeature(new_feat)
        
        rejected_layer.updateExtents()
        
        write_result_rej = QgsVectorFileWriter.writeAsVectorFormat(
            rejected_layer,
            rejected_output,
            "utf-8",
            rejected_layer.crs(),
            "GPKG"
        )
        
        if write_result_rej[0] == QgsVectorFileWriter.NoError:
            logging.info(f"🗑️ Formes rejetées sauvegardées : {os.path.basename(rejected_output)} ({len(rejected_features)} polygones)")
        else:
            logging.warning(f"⚠️ Erreur sauvegarde formes rejetées : {write_result_rej[1]}")

    # Statistiques finales détaillées (AVEC TEST6)
    logging.info("=" * 80)
    logging.info("RÉSULTATS ÉTAPE 6 - TESTS ANTI-BUTTE")
    logging.info("=" * 80)
    logging.info(f"Stratégie appliquée : share < {share_threshold}, pos < {position_threshold}")
    logging.info(f"Total enveloppes analysées: {stats['total']}")
    logging.info(f"✅ Formes acceptées: {stats['accepted']} ({stats['accepted']/stats['total']*100:.1f}%)")
    logging.info(f"❌ Rejetées - polygone unique: {stats['rejected_single']}")
    logging.info(f"❌ Rejetées - altitude manquante: {stats['rejected_altitude']}")
    logging.info(f"❌ Rejetées - dénivelé insuffisant: {stats['rejected_denivele']}")
    logging.info(f"❌ Rejetées - TEST1 (butte base/sommet): {stats['rejected_TEST1']}")
    logging.info(f"❌ Rejetées - TEST2 (dominance en bas): {stats['rejected_TEST2']}")
    logging.info(f"❌ Rejetées - TEST3 (butte extrême): {stats['rejected_TEST3']}")
    logging.info(f"❌ Rejetées - TEST4 (amplitude excessive): {stats['rejected_TEST4']}")
    logging.info(f"❌ Rejetées - TEST5 (micro-dépressions): {stats['rejected_TEST5']}")
    logging.info(f"❌ Rejetées - TEST6 (complexité excessive): {stats['rejected_TEST6']}")  # ← NOUVEAU
    
    total_rejections = (stats['rejected_single'] + stats['rejected_altitude'] + 
                       stats['rejected_denivele'] + stats['rejected_TEST1'] + 
                       stats['rejected_TEST2'] + stats['rejected_TEST3'] + 
                       stats['rejected_TEST4'] + stats['rejected_TEST5'] + 
                       stats['rejected_TEST6'])  # ← NOUVEAU
    
    logging.info(f"📈 Total rejets: {total_rejections}")
    logging.info(f"📈 Efficacité filtrage: {total_rejections/stats['total']*100:.1f}%")
    logging.info(f"🗑️ Formes rejetées sauvegardées: {len(rejected_features)} polygones")
    logging.info("=" * 80)

    # V1.1 : On ne charge plus depressions_verifiees dans le projet
    # (seule la couche finale comblee est affichee)
    logging.info(f"✅ Fichier genere : {os.path.basename(depressions_output)}")

    return depressions_output


# =============================================================================
# 9. ÉTAPE 7 : INTERFACE D'ÉTIQUETAGE 3 CLASSES SIMPLIFIÉE
# =============================================================================

class ImprovedTrainingDataDockWidget(QDockWidget):
    """Widget ancrable non-modal pour l'étiquetage 3 classes avec accès complet à QGIS"""
    
    closed = pyqtSignal()
    
    def __init__(self, depressions_layer, csv_output_folder, generate_training_files=False, parent=None):
        super().__init__("DEADKAT V1.0 - Étiquetage cartographique", parent)
        
        self.depressions_layer = depressions_layer
        self.csv_output_folder = csv_output_folder
        self.generate_training_files = generate_training_files
        self.current_index = 0
        
        # 3 listes pour les 3 classes
        self.naturelles = []
        self.anthropiques = []
        self.faux_positifs = []
        
        # Regrouper les dépressions par numéro
        self.depression_groups = self._group_depressions_by_number()
        self.depression_numbers = list(self.depression_groups.keys())
        
        # Configuration du dock widget
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.setFeatures(QDockWidget.DockWidgetClosable | 
                        QDockWidget.DockWidgetMovable | 
                        QDockWidget.DockWidgetFloatable)
        
        # Widget principal
        main_widget = QWidget()
        self.setWidget(main_widget)
        
        # Flag pour éviter les zooms multiples
        self.auto_zoom = True
        
        # Initialiser l'interface
        self._setup_ui(main_widget)
        
        # Afficher la première dépression
        if self.depression_numbers:
            self.show_depression(0)
    
    def closeEvent(self, event):
        """Gestion de la fermeture du widget"""
        if self.depressions_layer:
            self.depressions_layer.removeSelection()
        self.closed.emit()
        super().closeEvent(event)
    
    def _group_depressions_by_number(self):
        """Regroupe les features par leur attribut 'Numero'"""
        groups = {}
        
        for feature in self.depressions_layer.getFeatures():
            numero = safe_get_attribute(feature, 'Numero', None)
            if numero is not None:
                if numero not in groups:
                    groups[numero] = []
                groups[numero].append(feature)
        
        sorted_groups = dict(sorted(groups.items()))
        logging.info(f"📊 Regroupement: {self.depressions_layer.featureCount()} features → {len(sorted_groups)} dépressions")
        
        return sorted_groups
    
    def _setup_ui(self, parent_widget):
        """Configure l'interface utilisateur 3 classes simplifiée"""
        layout = QVBoxLayout(parent_widget)
        
        # === EN-TÊTE ===
        header_layout = QVBoxLayout()
        
        title_label = QLabel("🗺️ Étiquetage cartographique des dépressions")
        title_font = QFont()
        title_font.setPointSize(12)
        title_font.setBold(True)
        title_label.setFont(title_font)
        header_layout.addWidget(title_label)
        
        info_label = QLabel(
            "Interface non-modale : vous pouvez utiliser toutes les fonctionnalités QGIS !\n"
            "Naviguez entre les couches, changez la symbologie, utilisez les outils..."
        )
        info_label.setWordWrap(True)
        info_label.setStyleSheet("QLabel { background-color: #e3f2fd; padding: 10px; border-radius: 5px; }")
        header_layout.addWidget(info_label)
        
        layout.addLayout(header_layout)
        
        # === BARRE DE PROGRESSION ===
        progress_group = QGroupBox("Progression")
        progress_layout = QVBoxLayout()
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximum(len(self.depression_numbers))
        progress_layout.addWidget(self.progress_bar)
        
        self.progress_label = QLabel()
        self._update_progress_label()
        progress_layout.addWidget(self.progress_label)
        
        progress_group.setLayout(progress_layout)
        layout.addWidget(progress_group)
        
        # === INFORMATIONS SUR LA DÉPRESSION ===
        info_group = QGroupBox("Dépression actuelle")
        info_layout = QVBoxLayout()
        
        self.info_text = QTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setMaximumHeight(120)
        info_layout.addWidget(self.info_text)
        
        # Bouton pour re-zoomer sur la dépression
        rezoom_btn = QPushButton("🔍 Recentrer sur cette dépression")
        rezoom_btn.clicked.connect(self.rezoom_current)
        rezoom_btn.setStyleSheet("QPushButton { background-color: #2196F3; color: white; padding: 6px; }")
        info_layout.addWidget(rezoom_btn)
        
        info_group.setLayout(info_layout)
        layout.addWidget(info_group)
        
        # === CONTRÔLES DE NAVIGATION ===
        nav_group = QGroupBox("Navigation")
        nav_layout = QHBoxLayout()
        
        self.prev_button = QPushButton("◀ Précédent")
        self.prev_button.clicked.connect(self.previous_depression)
        nav_layout.addWidget(self.prev_button)
        
        nav_layout.addWidget(QLabel("Aller à :"))
        self.goto_spinbox = QSpinBox()
        self.goto_spinbox.setMinimum(1)
        self.goto_spinbox.setMaximum(len(self.depression_numbers))
        self.goto_spinbox.valueChanged.connect(self.goto_depression)
        nav_layout.addWidget(self.goto_spinbox)
        
        self.next_button = QPushButton("Suivant ▶")
        self.next_button.clicked.connect(self.next_depression)
        nav_layout.addWidget(self.next_button)
        
        nav_layout.addStretch()
        
        self.auto_zoom_checkbox = QCheckBox("Zoom auto")
        self.auto_zoom_checkbox.setChecked(True)
        self.auto_zoom_checkbox.stateChanged.connect(self.toggle_auto_zoom)
        nav_layout.addWidget(self.auto_zoom_checkbox)
        
        nav_group.setLayout(nav_layout)
        layout.addWidget(nav_group)
        
        # === CONTRÔLE DE TRANSPARENCE SIMPLIFIÉ ===
        transparency_group = QGroupBox("🗺️ Transparence de la couche")
        transparency_layout = QVBoxLayout()
        
        # Slider horizontal uniquement
        slider_container = QHBoxLayout()
        
        slider_label_left = QLabel("Opaque")
        slider_label_left.setStyleSheet("font-weight: bold; color: #2196F3;")
        
        self.transparency_slider = QSlider(Qt.Horizontal)
        self.transparency_slider.setMinimum(0)
        self.transparency_slider.setMaximum(100)
        self.transparency_slider.setValue(70)
        self.transparency_slider.setTickPosition(QSlider.TicksBelow)
        self.transparency_slider.setTickInterval(10)
        self.transparency_slider.setStyleSheet("""
            QSlider::groove:horizontal {
                border: 1px solid #999;
                height: 8px;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #2196F3, stop:1 #E3F2FD);
                margin: 2px 0;
                border-radius: 4px;
            }
            QSlider::handle:horizontal {
                background: #1976D2;
                border: 2px solid #0D47A1;
                width: 18px;
                margin: -5px 0;
                border-radius: 9px;
            }
            QSlider::handle:horizontal:hover {
                background: #2196F3;
            }
        """)
        
        slider_label_right = QLabel("Transparent")
        slider_label_right.setStyleSheet("font-weight: bold; color: #E3F2FD;")
        
        self.transparency_value_label = QLabel("70%")
        self.transparency_value_label.setStyleSheet("font-weight: bold; font-size: 14px; color: #1976D2;")
        self.transparency_value_label.setMinimumWidth(50)
        self.transparency_value_label.setAlignment(Qt.AlignCenter)
        
        slider_container.addWidget(slider_label_left)
        slider_container.addWidget(self.transparency_slider)
        slider_container.addWidget(slider_label_right)
        slider_container.addWidget(self.transparency_value_label)
        
        transparency_layout.addLayout(slider_container)
        transparency_group.setLayout(transparency_layout)
        layout.addWidget(transparency_group)
        
        # Connexion slider
        self.transparency_slider.valueChanged.connect(self.update_transparency)
        
        # Appliquer transparence initiale
        self.update_transparency(70)
        
        # === CLASSIFICATION 3 CLASSES ===
        class_group = QGroupBox("Classification")
        class_layout = QVBoxLayout()
        
        # Boutons radio
        self.radio_naturelle = QRadioButton("🔵 Dépression naturelle supposée (doline, gouffre, perte...)")
        self.radio_naturelle.setStyleSheet("font-weight: bold; color: #002395; font-size: 12px;")
        
        self.radio_anthropique = QRadioButton("🟢 Dépression anthropique supposée (mare, carrière, artefact bâti ...)")
        self.radio_anthropique.setStyleSheet("font-weight: bold; color: #2e7d32; font-size: 12px;")
        
        self.radio_faux = QRadioButton("🔴 Faux positif (butte, artefact...)")
        self.radio_faux.setStyleSheet("font-weight: bold; color: #c62828; font-size: 12px;")
        
        # Par défaut : Naturelle sélectionnée
        self.radio_naturelle.setChecked(True)
        
        class_layout.addWidget(self.radio_naturelle)
        class_layout.addWidget(self.radio_anthropique)
        class_layout.addWidget(self.radio_faux)
        
        # Boutons d'action
        button_layout = QHBoxLayout()
        
        prev_btn = QPushButton("← Précédent")
        prev_btn.clicked.connect(self.previous_depression)
        prev_btn.setStyleSheet("QPushButton { padding: 8px; }")
        
        self.validate_button = QPushButton("VALIDER →")
        self.validate_button.clicked.connect(self.validate_current)
        self.validate_button.setDefault(True)
        self.validate_button.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                font-weight: bold;
                padding: 8px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
        """)
        
        skip_btn = QPushButton("⏭️ PASSER")
        skip_btn.clicked.connect(self.skip_current)
        skip_btn.setStyleSheet("""
            QPushButton {
                background-color: #ff9800;
                color: white;
                font-weight: bold;
                padding: 8px;
            }
            QPushButton:hover {
                background-color: #e68900;
            }
        """)
        
        button_layout.addWidget(prev_btn)
        button_layout.addStretch()
        button_layout.addWidget(self.validate_button)
        button_layout.addWidget(skip_btn)
        
        class_layout.addLayout(button_layout)
        class_group.setLayout(class_layout)
        layout.addWidget(class_group)
        
        # === STATISTIQUES ===
        stats_group = QGroupBox("Statistiques")
        stats_layout = QHBoxLayout()
        
        self.stats_label = QLabel()
        self._update_stats_label()
        stats_layout.addWidget(self.stats_label)
        
        stats_group.setLayout(stats_layout)
        layout.addWidget(stats_group)
        
        # === BOUTON SAUVEGARDE ===
        action_layout = QHBoxLayout()
        
        self.finish_button = QPushButton("💾 SAUVEGARDER ET FERMER")
        self.finish_button.clicked.connect(self.finish_labeling)
        self.finish_button.setStyleSheet("""
            QPushButton {
                background-color: #2196F3;
                color: white;
                font-weight: bold;
                padding: 10px;
                font-size: 12px;
            }
        """)
        action_layout.addWidget(self.finish_button)
        
        layout.addLayout(action_layout)
        
        # Astuce
        tip_label = QLabel("💡 Astuce : Ancrez ce panneau sur le côté pour libérer l'espace de travail")
        tip_label.setStyleSheet("QLabel { color: #666; font-style: italic; font-size: 10px; }")
        layout.addWidget(tip_label)
        
        # Espacement
        layout.addStretch()
    
    def update_transparency(self, value):
        """Met à jour la transparence de la couche"""
        opacity = value / 100.0
        self.depressions_layer.setOpacity(opacity)
        self.depressions_layer.triggerRepaint()
        iface.mapCanvas().refresh()
        self.transparency_value_label.setText(f"{value}%")
    
    def show_depression(self, index):
        """Affiche une dépression spécifique"""
        if 0 <= index < len(self.depression_numbers):
            self.current_index = index
            depression_number = self.depression_numbers[index]
            features = self.depression_groups[depression_number]
            
            self._update_depression_info(depression_number, features)
            self._select_and_zoom_depression(features)
            self._update_controls()
            self._update_progress_label()
            self._update_stats_label()
    
    def _update_depression_info(self, depression_number, features):
        """Met à jour les informations affichées sur la dépression"""
        total_area = sum(safe_get_attribute(f, 'Area', 0) for f in features)
        total_perimeter = sum(safe_get_attribute(f, 'Perimeter', 0) for f in features)
        altitudes = [safe_get_attribute(f, 'Altitude', 0) for f in features if safe_get_attribute(f, 'Altitude', None) is not None]
        
        if altitudes:
            min_alt = min(altitudes)
            max_alt = max(altitudes)
            diff_alt = max_alt - min_alt
        else:
            min_alt = max_alt = diff_alt = 0
        
        info = f"📍 Dépression {depression_number}\n\n"
        info += f"Polygones : {len(features)} | "
        info += f"Surface : {total_area:.1f} m² | "
        info += f"Périmètre : {total_perimeter:.1f} m\n"
        info += f"Alt min : {min_alt:.1f} m | "
        info += f"Alt max : {max_alt:.1f} m | "
        info += f"Dénivelé : {diff_alt:.1f} m"
        
        self.info_text.setText(info)
    
    def _select_and_zoom_depression(self, features):
        """Sélectionne et zoom sur la dépression dans QGIS"""
        if not features:
            return
        
        self.depressions_layer.removeSelection()
        feature_ids = [f.id() for f in features]
        self.depressions_layer.selectByIds(feature_ids)
        
        if self.auto_zoom:
            self._zoom_to_features(features)
    
    def rezoom_current(self):
        """Recentre sur la dépression actuelle"""
        if self.current_index < len(self.depression_numbers):
            depression_number = self.depression_numbers[self.current_index]
            features = self.depression_groups[depression_number]
            self._zoom_to_features(features)
    
    def _zoom_to_features(self, features):
        """Effectue le zoom sur les features à échelle fixe 1:5000"""
        if not features:
            return
        
        # Calculer le centroïde de l'ensemble des features
        combined_bbox = QgsRectangle()
        for feature in features:
            if combined_bbox.isEmpty():
                combined_bbox = feature.geometry().boundingBox()
            else:
                combined_bbox.combineExtentWith(feature.geometry().boundingBox())
        
        center_x = combined_bbox.center().x()
        center_y = combined_bbox.center().y()
        
        # Échelle fixe 1:2500
        # À 1:2500, 1 cm sur l'écran = 25 m sur le terrain
        # On calcule une emprise de ~500m x 500m (visible confortablement)
        scale_factor = 1000
        
        canvas = iface.mapCanvas()
        
        # Calculer la taille de l'emprise en mètres pour l'échelle 1:2500
        # Basé sur la taille du canvas en pixels et la résolution de l'écran
        canvas_width = canvas.width()
        canvas_height = canvas.height()
        
        # Approximation : 96 DPI standard, donc ~0.0264 m par pixel à l'écran
        # À l'échelle 1:2500, chaque pixel représente environ 2.6 m sur le terrain
        meters_per_pixel = scale_factor / 3780  # 3780 pixels par mètre à 96 DPI
        
        half_width = (canvas_width * meters_per_pixel) / 2
        half_height = (canvas_height * meters_per_pixel) / 2
        
        # Créer l'emprise centrée sur la dépression
        new_extent = QgsRectangle(
            center_x - half_width,
            center_y - half_height,
            center_x + half_width,
            center_y + half_height
        )
        
        canvas.setExtent(new_extent)
        canvas.zoomScale(scale_factor)
        canvas.refresh()
    
    def _update_controls(self):
        """Met à jour l'état des contrôles de navigation"""
        self.prev_button.setEnabled(self.current_index > 0)
        self.next_button.setEnabled(self.current_index < len(self.depression_numbers) - 1)
        self.goto_spinbox.blockSignals(True)
        self.goto_spinbox.setValue(self.current_index + 1)
        self.goto_spinbox.blockSignals(False)
        self.progress_bar.setValue(self.current_index + 1)
    
    def _update_progress_label(self):
        """Met à jour le label de progression"""
        total = len(self.depression_numbers)
        current = self.current_index + 1
        labeled = len(self.naturelles) + len(self.anthropiques) + len(self.faux_positifs)
        
        self.progress_label.setText(
            f"Dépression {current}/{total} | "
            f"Étiquetées : {labeled}/{total}"
        )
    
    def _update_stats_label(self):
        """Met à jour les statistiques"""
        total = len(self.depression_numbers)
        labeled = len(self.naturelles) + len(self.anthropiques) + len(self.faux_positifs)
        skipped = total - labeled
        
        self.stats_label.setText(
            f"🔵 Dépressions naturelles : {len(self.naturelles)} | "
            f"🟢 Dépressions anthropiques: {len(self.anthropiques)} | "
            f"🔴 Faux positifs: {len(self.faux_positifs)} | "
            f"⏭️ Passées: {skipped}"
        )
    
    def validate_current(self):
        """Valide la classification et passe à la suivante - AVEC MODIFICATION POSSIBLE"""
        if self.current_index < len(self.depression_numbers):
            depression_number = self.depression_numbers[self.current_index]
            features = self.depression_groups[depression_number]
            record = self._aggregate_depression_features(depression_number, features)
            
            # Supprimer l'ancienne classification si elle existe
            self.naturelles = [d for d in self.naturelles if d['Numero'] != depression_number]
            self.anthropiques = [d for d in self.anthropiques if d['Numero'] != depression_number]
            self.faux_positifs = [d for d in self.faux_positifs if d['Numero'] != depression_number]
            
            # Ajouter la nouvelle classification
            if self.radio_naturelle.isChecked():
                self.naturelles.append(record)
                logging.info(f"🔵 Dépression {depression_number} → NATURELLE")
            elif self.radio_anthropique.isChecked():
                self.anthropiques.append(record)
                logging.info(f"🟢 Dépression {depression_number} → ANTHROPIQUE")
            elif self.radio_faux.isChecked():
                self.faux_positifs.append(record)
                logging.info(f"🔴 Dépression {depression_number} → FAUX POSITIF")
            
            # Réinitialiser sélection par défaut
            self.radio_naturelle.setChecked(True)
            
            self._update_stats_label()
            self.next_depression()
    
    def skip_current(self):
        """Passe à la dépression suivante sans étiqueter"""
        self.next_depression()
    
    def next_depression(self):
        """Passe à la dépression suivante"""
        if self.current_index < len(self.depression_numbers) - 1:
            self.show_depression(self.current_index + 1)
    
    def previous_depression(self):
        """Retourne à la dépression précédente"""
        if self.current_index > 0:
            self.show_depression(self.current_index - 1)
    
    def goto_depression(self, value):
        """Va directement à une dépression spécifique"""
        self.show_depression(value - 1)
    
    def toggle_auto_zoom(self, state):
        """Active/désactive le zoom automatique"""
        self.auto_zoom = state == Qt.Checked
    
    def _aggregate_depression_features(self, depression_number, features):
        """Agrège les caractéristiques de toutes les features d'une dépression"""
        total_area = sum(safe_get_attribute(f, 'Area', 0) for f in features)
        total_perimeter = sum(safe_get_attribute(f, 'Perimeter', 0) for f in features)
        altitudes = [safe_get_attribute(f, 'Altitude', 0) for f in features if safe_get_attribute(f, 'Altitude', None) is not None]
        
        if altitudes:
            min_alt = min(altitudes)
            max_alt = max(altitudes)
            diff_alt = max_alt - min_alt
        else:
            min_alt = max_alt = diff_alt = 0
        
        # 🆕 CALCULER LA CIRCULARITÉ
        if total_perimeter > 0:
            circularity = (4 * 3.14159 * total_area) / (total_perimeter ** 2)
        else:
            circularity = 0.0

        record = {
            'Numero': depression_number,
            'Area': total_area,
            'Perimeter': total_perimeter,
            'DiffAlt': diff_alt,
            'AltMin': min_alt,
            'AltMax': max_alt,
            'Num_Polygons': len(features),
            'Circularity': circularity
        }

        # V1.1 : propagation des 18 features BD TOPO + 2 features géologie
        # On prend la valeur du premier feature du groupe (toutes les features d'une
        # même dépression ont le même contexte anthropique et géologique).
        if features:
            first_feat = features[0]
            for short_name in BDTOPO_ALL_SHORT:
                val = safe_get_attribute(first_feat, short_name, None)
                if val is not None and val != '':
                    record[short_name] = val

        return record
    
    def finish_labeling(self):
        """Termine l'étiquetage et sauvegarde les données"""
        self.depressions_layer.removeSelection()
        
        if self.generate_training_files and HAS_PANDAS:
            logging.info("💾 Sauvegarde des fichiers CSV d'entraînement (3 classes)...")
            
            csv_files_created = []
            shp_files_created = []
            
            # Sauvegarder les 3 catégories
            if self.naturelles:
                nat_df = pd.DataFrame(self.naturelles)
                nat_csv = os.path.join(self.csv_output_folder, 'entrainement_dep_naturelles.csv')
                nat_df.to_csv(nat_csv, index=False)
                csv_files_created.append(nat_csv)
                logging.info(f"✅ Naturelles CSV: {nat_csv} ({len(self.naturelles)} dépressions)")
                
                # Créer shapefile
                nat_shp = self._create_shapefile_from_labels(
                    self.naturelles, 
                    'etiquetees_dep_naturelles.gpkg',
                    'Dépressions naturelles',
                    'Naturelle'
                )
                if nat_shp:
                    shp_files_created.append(('naturelles', nat_shp))
            
            if self.anthropiques:
                anth_df = pd.DataFrame(self.anthropiques)
                anth_csv = os.path.join(self.csv_output_folder, 'entrainement_dep_anthropiques.csv')
                anth_df.to_csv(anth_csv, index=False)
                csv_files_created.append(anth_csv)
                logging.info(f"✅ Anthropiques CSV: {anth_csv} ({len(self.anthropiques)} dépressions)")
                
                # Créer shapefile
                anth_shp = self._create_shapefile_from_labels(
                    self.anthropiques, 
                    'etiquetees_dep_anthropiques.gpkg',
                    'Dépressions anthropiques',
                    'Anthropique'
                )
                if anth_shp:
                    shp_files_created.append(('anthropiques', anth_shp))
            
            if self.faux_positifs:
                faux_df = pd.DataFrame(self.faux_positifs)
                faux_csv = os.path.join(self.csv_output_folder, 'entrainement_faux_positifs.csv')
                faux_df.to_csv(faux_csv, index=False)
                csv_files_created.append(faux_csv)
                logging.info(f"✅ Faux positifs CSV: {faux_csv} ({len(self.faux_positifs)} dépressions)")
                
                # Créer shapefile
                faux_shp = self._create_shapefile_from_labels(
                    self.faux_positifs, 
                    'etiquetees_faux_positifs.gpkg',
                    'Faux positifs',
                    'Faux'
                )
                if faux_shp:
                    shp_files_created.append(('faux_positifs', faux_shp))
            
            # ========================================
            # CHARGEMENT AUTOMATIQUE AVEC SYMBOLOGIE
            # ========================================
            if shp_files_created:
                logging.info("🎨 Chargement des couches avec symbologie...")
                self._load_shapefiles_with_style(shp_files_created)
            
            # Message de confirmation
            total_labeled = len(self.naturelles) + len(self.anthropiques) + len(self.faux_positifs)
            total_possible = len(self.depression_numbers)
            
            message = f"🎉 Étiquetage terminé !\n\n"
            message += f"📊 Statistiques :\n"
            message += f"• Dépressions étiquetées : {total_labeled}/{total_possible}\n"
            message += f"• 🔵 Naturelles : {len(self.naturelles)}\n"
            message += f"• 🟢 Anthropiques : {len(self.anthropiques)}\n"
            message += f"• 🔴 Faux positifs : {len(self.faux_positifs)}\n"
            message += f"• ⏭️ Non étiquetées : {total_possible - total_labeled}\n\n"
            
            if total_labeled > 0:
                message += f"💾 Fichiers créés :\n"
                
                message += f"\n📋 Données tabulaires :\n"
                for csv_file in csv_files_created:
                    message += f"• {os.path.basename(csv_file)}\n"
                
                if shp_files_created:
                    message += f"\n🗺️ Couches cartographiques :\n"
                    for category, shp_file in shp_files_created:
                        message += f"• {os.path.basename(shp_file)}\n"
                    message += f"\n✅ Les couches ont été ajoutées au projet QGIS avec symbologie !"
                
                message += f"\n🤖 Prochaine étape :\n"
                message += f"Utilisez 'ENTRAÎNER un modèle ML' avec vos CSV"
            else:
                message += "⚠️ Aucune dépression n'a été étiquetée."
            
            QMessageBox.information(self, "Étiquetage terminé", message)
            
        elif not self.generate_training_files:
            logging.info("👁️ Fin de la session de visualisation")
            
            total_viewed = len(self.depression_numbers)
            total_labeled = len(self.naturelles) + len(self.anthropiques) + len(self.faux_positifs)
            
            message = f"👁️ Session de visualisation terminée !\n\n"
            message += f"📊 Statistiques :\n"
            message += f"• Dépressions visualisées : {total_viewed}\n"
            message += f"• Classifications temporaires : {total_labeled}\n\n"
            message += f"ℹ️ Mode visualisation - aucun fichier généré.\n\n"
            message += f"Pour créer des données d'entraînement, utilisez :\n"
            message += f"🏷️ CRÉER des données d'entraînement ML"
            
            QMessageBox.information(self, "Visualisation terminée", message)
        
        else:
            logging.error("❌ Pandas non disponible")
            QMessageBox.warning(self, "Erreur", "Pandas nécessaire pour sauvegarder")
        
        logging.info("🔒 Fermeture de l'interface d'étiquetage")
        self.close()
    
    def _load_shapefiles_with_style(self, shp_files_list):
        """
        Charge les shapefiles dans QGIS avec symbologie colorée
        
        Args:
            shp_files_list: Liste de tuples (category, shp_path)
                           category peut être: 'naturelles', 'anthropiques', 'faux_positifs'
        """
        from qgis.core import QgsSymbol, QgsSimpleFillSymbolLayer, QgsRendererCategory, QgsCategorizedSymbolRenderer
        from PyQt5.QtGui import QColor
        
        try:
            project = QgsProject.instance()
            
            # Définir les couleurs selon la catégorie
            color_mapping = {
                'naturelles': {
                    'fill': QColor(0, 35, 149, 180),        # Bleu Klein (#002395) avec transparence
                    'outline': QColor(0, 20, 90, 255),      # Bleu Klein foncé
                    'name': '🔵 Dépressions naturelles'
                },
                'anthropiques': {
                    'fill': QColor(76, 175, 80, 180),       # Vert (#4CAF50) avec transparence
                    'outline': QColor(27, 94, 32, 255),     # Vert foncé
                    'name': '🟢 Dépressions anthropiques'
                },
                'faux_positifs': {
                    'fill': QColor(244, 67, 54, 180),       # Rouge (#F44336) avec transparence
                    'outline': QColor(183, 28, 28, 255),    # Rouge foncé
                    'name': '🔴 Faux positifs'
                }
            }
            
            for category, shp_path in shp_files_list:
                if not os.path.exists(shp_path):
                    logging.warning(f"Fichier introuvable: {shp_path}")
                    continue
                
                # Récupérer les couleurs
                colors = color_mapping.get(category)
                if not colors:
                    logging.warning(f"Catégorie inconnue: {category}")
                    continue
                
                # Charger la couche
                layer_name = colors['name']
                layer = QgsVectorLayer(shp_path, layer_name, "ogr")
                
                if not layer.isValid():
                    logging.error(f"Couche invalide: {shp_path}")
                    continue
                
                # Créer le symbole
                symbol = QgsSymbol.defaultSymbol(layer.geometryType())
                
                # Configurer le remplissage
                symbol_layer = QgsSimpleFillSymbolLayer()
                symbol_layer.setFillColor(colors['fill'])
                symbol_layer.setStrokeColor(colors['outline'])
                symbol_layer.setStrokeWidth(0.6)
                
                # Appliquer le symbole
                symbol.changeSymbolLayer(0, symbol_layer)
                layer.renderer().setSymbol(symbol)
                
                # Ajouter au projet
                project.addMapLayer(layer)
                
                logging.info(f"✅ Couche chargée avec symbologie: {layer_name}")
            
            # Rafraîchir la carte
            iface.mapCanvas().refresh()
            
            logging.info("🎨 Toutes les couches ont été chargées avec symbologie")
            
        except Exception as e:
            logging.error(f"❌ Erreur chargement symbologie: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
    
    def _create_shapefile_from_labels(self, labeled_data, filename, layer_name, label_value):
        """
        Crée un shapefile à partir des données étiquetées
        
        Args:
            labeled_data: Liste des records (dict) à sauvegarder
            filename: Nom du fichier .shp de sortie
            layer_name: Nom de la couche mémoire
            label_value: Valeur du champ Label ('Naturelle', 'Anthropique', 'Faux')
        """
        try:
            layer = QgsVectorLayer("MultiPolygon?crs=EPSG:2154", layer_name, "memory")
            provider = layer.dataProvider()
            
            fields = QgsFields()
            fields.append(QgsField('Numero', QVariant.String))
            fields.append(QgsField('Area', QVariant.Double))
            fields.append(QgsField('Perimeter', QVariant.Double))
            fields.append(QgsField('DiffAlt', QVariant.Double))
            fields.append(QgsField('AltMin', QVariant.Double))
            fields.append(QgsField('AltMax', QVariant.Double))
            fields.append(QgsField('Num_Polygons', QVariant.Int))
            fields.append(QgsField('Label', QVariant.String))
            
            provider.addAttributes(fields)
            layer.updateFields()
            
            features_to_add = []
            
            for record in labeled_data:
                numero = record['Numero']
                depression_features = self.depression_groups.get(numero, [])
                if not depression_features:
                    continue
                
                geometries = [f.geometry() for f in depression_features]
                if geometries:
                    merged_geom = QgsGeometry.unaryUnion(geometries)
                    
                    new_feature = QgsFeature(layer.fields())
                    new_feature.setGeometry(merged_geom)
                    new_feature['Numero'] = record['Numero']
                    new_feature['Area'] = record['Area']
                    new_feature['Perimeter'] = record['Perimeter']
                    new_feature['DiffAlt'] = record['DiffAlt']
                    new_feature['AltMin'] = record['AltMin']
                    new_feature['AltMax'] = record['AltMax']
                    new_feature['Num_Polygons'] = record['Num_Polygons']
                    new_feature['Label'] = label_value
                    
                    features_to_add.append(new_feature)
            
            if not features_to_add:
                logging.warning(f"Aucune feature à sauvegarder pour {filename}")
                return None
            
            provider.addFeatures(features_to_add)
            layer.updateExtents()
            
            output_path = os.path.join(self.csv_output_folder, filename)
            write_result = QgsVectorFileWriter.writeAsVectorFormat(
                layer, output_path, "utf-8", layer.crs(), "GPKG"
            )
            
            if write_result[0] != QgsVectorFileWriter.NoError:
                logging.error(f"Erreur sauvegarde shapefile: {write_result[1]}")
                return None
            
            if not os.path.exists(output_path):
                logging.error(f"Fichier shapefile non créé: {output_path}")
                return None
            
            logging.info(f"✅ Shapefile créé: {output_path} ({len(features_to_add)} dépressions)")
            return output_path
            
        except Exception as e:
            logging.error(f"❌ Erreur création shapefile {filename}: {str(e)}")
            import traceback
            logging.error(f"Traceback: {traceback.format_exc()}")
            return None


def step8_create_training_interface(depressions_layer, output_folder, csv_output_folder=None, generate_training_files=False):
    """
    Étape 7 : Lance l'interface d'étiquetage cartographique 3 classes
    
    Args:
        generate_training_files (bool): Si True, génère les fichiers CSV d'entraînement
    """
    global global_dock_widget
    
    logging.info("Étape 7 : Interface d'étiquetage 3 classes")
    
    if not depressions_layer or not depressions_layer.isValid():
        raise Exception("Couche de dépressions invalide")
    
    feature_count = depressions_layer.featureCount()
    if feature_count == 0:
        raise Exception("Aucune dépression trouvée")
    
    logging.info(f"📊 Features à examiner : {feature_count}")
    logging.info(f"🏷️ Mode training : {'OUI' if generate_training_files else 'NON'}")
    
    # Ajouter la couche au projet
    if not QgsProject.instance().mapLayer(depressions_layer.id()):
        QgsProject.instance().addMapLayer(depressions_layer)
    
    iface.setActiveLayer(depressions_layer)
    
    def launch_labeling_interface():
        global global_dock_widget
        
        try:
            logging.info("🔧 Lancement interface...")
            
            if global_dock_widget:
                try:
                    global_dock_widget.close()
                except:
                    pass
            
            csv_folder = csv_output_folder if csv_output_folder else output_folder
            global_dock_widget = ImprovedTrainingDataDockWidget(
                depressions_layer, 
                csv_folder,
                generate_training_files
            )
            
            iface.addDockWidget(Qt.RightDockWidgetArea, global_dock_widget)
            global_dock_widget.setFloating(True)
            global_dock_widget.resize(480, 850)
            global_dock_widget.move(200, 50)
            
            global_dock_widget.show()
            global_dock_widget.raise_()
            global_dock_widget.activateWindow()
            
            logging.info("✅ Interface lancée")
            return global_dock_widget
            
        except Exception as e:
            logging.error(f"❌ Erreur lancement: {e}")
            QMessageBox.critical(None, "Erreur", f"Erreur:\n{e}")
            return None
    
    # Message informatif
    reply = QMessageBox.question(
        None,
        "DEADKAT V1.0 - Interface 3 classes",
        f"🏷️ Interface d'étiquetage 3 classes prête !\n\n"
        f"• Features détectées : {feature_count}\n"
        f"• Dossier : {output_folder}\n\n"
        f"🔵 Naturelle | 🟢 Anthropique | 🔴 Faux profil\n\n"
        f"Cliquez sur 'Oui' pour lancer.",
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.Yes
    )
    
    if reply == QMessageBox.Yes:
        dock = launch_labeling_interface()
        if dock:
            logging.info("✅ Étape 7 terminée")
            return dock
        else:
            raise Exception("Échec lancement interface")
    else:
        logging.info("❌ Annulé par l'utilisateur")
        raise Exception("Interface annulée")

# =============================================================================
# 10. MACHINE LEARNING - RANDOM FOREST OPTIMISÉ
# =============================================================================

def calculate_elevation_context_features(depression_geom, mnt_path):
    """Calcule les features de contexte topographique (anti-buttes)"""
    if not HAS_NUMPY:
        logging.warning("NumPy manquant - Features topographiques limitées")
        return {'elevation_relative_10m': 0, 'elevation_relative_25m': 0, 'elevation_relative_50m': 0,
                'bowl_shape_index': 0, 'min_elevation_rank': 0.5}
    
    try:
        import processing
        from osgeo import gdal
        import tempfile
        import os
        
        # Calculer le centroïde et créer des buffers
        centroid = depression_geom.centroid().asPoint()
        bbox = depression_geom.boundingBox()
        
        # Étendre la bbox pour inclure les zones de contexte
        buffer_size = 60  # 10m plus grand que le buffer max (50m)
        extended_bbox = QgsRectangle(
            bbox.xMinimum() - buffer_size,
            bbox.yMinimum() - buffer_size,
            bbox.xMaximum() + buffer_size,
            bbox.yMaximum() + buffer_size
        )
        
        # Clipper le MNT sur la zone étendue
        temp_clip = tempfile.mktemp(suffix='.tif')
        
        clip_params = {
            'INPUT': mnt_path,
            'PROJWIN': f"{extended_bbox.xMinimum()},{extended_bbox.xMaximum()},{extended_bbox.yMaximum()},{extended_bbox.yMinimum()}",
            'OUTPUT': temp_clip
        }
        processing.run("gdal:cliprasterbyextent", clip_params)
        
        # Ouvrir le raster clippé
        ds = gdal.Open(temp_clip)
        if not ds:
            raise Exception("Impossible d'ouvrir le MNT clippé")
        
        band = ds.GetRasterBand(1)
        gt = ds.GetGeoTransform()
        arr = band.ReadAsArray()
        
        if arr is None:
            raise Exception("Données raster vides")
        
        # Fonction pour convertir coordonnées geo -> pixel
        def geo_to_pixel(x, y):
            px = int((x - gt[0]) / gt[1])
            py = int((y - gt[3]) / gt[5])
            return px, py
        
        # Fonction pour calculer altitude moyenne dans un buffer
        def get_elevation_in_buffer(center_x, center_y, buffer_radius):
            px_center, py_center = geo_to_pixel(center_x, center_y)
            pixel_radius = int(buffer_radius / abs(gt[1]))  # Convertir en pixels
            
            elevations = []
            for dy in range(-pixel_radius, pixel_radius + 1):
                for dx in range(-pixel_radius, pixel_radius + 1):
                    px, py = px_center + dx, py_center + dy
                    if 0 <= px < arr.shape[1] and 0 <= py < arr.shape[0]:
                        # Vérifier si on est dans le cercle
                        dist = np.sqrt(dx*dx + dy*dy) * abs(gt[1])
                        if dist <= buffer_radius:
                            elev = arr[py, px]
                            if not np.isnan(elev) and elev != band.GetNoDataValue():
                                elevations.append(elev)
            
            return np.mean(elevations) if elevations else 0
        
        # Calculer altitude de la dépression (point le plus bas)
        depression_pixels = []
        if depression_geom.isMultipart():
            polygons = depression_geom.asMultiPolygon()
            for polygon in polygons:
                for ring in polygon:
                    for point in ring:
                        px, py = geo_to_pixel(point.x(), point.y())
                        if 0 <= px < arr.shape[1] and 0 <= py < arr.shape[0]:
                            depression_pixels.append(arr[py, px])
        else:
            polygon = depression_geom.asPolygon()
            for ring in polygon:
                for point in ring:
                    px, py = geo_to_pixel(point.x(), point.y())
                    if 0 <= px < arr.shape[1] and 0 <= py < arr.shape[0]:
                        depression_pixels.append(arr[py, px])
        
        # Filtrer les valeurs valides
        valid_pixels = [p for p in depression_pixels if not np.isnan(p) and p != band.GetNoDataValue()]
        if not valid_pixels:
            raise Exception("Aucune valeur d'altitude valide dans la dépression")
        
        depression_min_elev = float(np.min(valid_pixels))
        depression_mean_elev = float(np.mean(valid_pixels))
        
        # Calculer les altitudes moyennes dans les buffers
        elev_10m = get_elevation_in_buffer(centroid.x(), centroid.y(), 10)
        elev_25m = get_elevation_in_buffer(centroid.x(), centroid.y(), 25) 
        elev_50m = get_elevation_in_buffer(centroid.x(), centroid.y(), 50)
        
        # Features relatives (négatif = dépression, positif = butte)
        elevation_relative_10m = depression_mean_elev - elev_10m
        elevation_relative_25m = depression_mean_elev - elev_25m
        elevation_relative_50m = depression_mean_elev - elev_50m
        
        # Indice de forme cuvette vs cône
        # Ratio entre dénivelé interne et dénivelé externe
        internal_range = float(np.max(valid_pixels) - np.min(valid_pixels))
        external_range = max(elev_10m - depression_mean_elev, 0.1)  # Éviter division par 0
        bowl_shape_index = internal_range / external_range
        
        # Rang du minimum (0 = vraiment le point le plus bas dans la zone)
        all_elevations_50m = []
        px_center, py_center = geo_to_pixel(centroid.x(), centroid.y())
        pixel_radius_50 = int(50 / abs(gt[1]))
        
        for dy in range(-pixel_radius_50, pixel_radius_50 + 1):
            for dx in range(-pixel_radius_50, pixel_radius_50 + 1):
                px, py = px_center + dx, py_center + dy
                if 0 <= px < arr.shape[1] and 0 <= py < arr.shape[0]:
                    dist = np.sqrt(dx*dx + dy*dy) * abs(gt[1])
                    if dist <= 50:
                        elev = arr[py, px]
                        if not np.isnan(elev) and elev != band.GetNoDataValue():
                            all_elevations_50m.append(elev)
        
        if all_elevations_50m:
            sorted_elevs = sorted(all_elevations_50m)
            min_rank = sum(1 for e in sorted_elevs if e < depression_min_elev) / len(sorted_elevs)
        else:
            min_rank = 0.5
        
        # Nettoyage
        ds = None
        try:
            os.remove(temp_clip)
        except:
            pass
        
        return {
            'elevation_relative_10m': float(elevation_relative_10m),
            'elevation_relative_25m': float(elevation_relative_25m), 
            'elevation_relative_50m': float(elevation_relative_50m),
            'bowl_shape_index': float(bowl_shape_index),
            'min_elevation_rank': float(min_rank)
        }
        
    except Exception as e:
        logging.warning(f"Erreur calcul features topographiques: {str(e)}")
        return {'elevation_relative_10m': 0, 'elevation_relative_25m': 0, 'elevation_relative_50m': 0,
                'bowl_shape_index': 0, 'min_elevation_rank': 0.5}


def calculate_karst_morphometry_features(depression_geom, depression_attributes):
    """Calcule les features de morphométrie karstique spécialisées"""
    try:
        # Récupérer les attributs de base
        area = float(depression_attributes.get('Area', 0))
        perimeter = float(depression_attributes.get('Perimeter', 0))
        diff_alt = float(depression_attributes.get('DiffAlt', 0))
        
        if area <= 0 or perimeter <= 0:
            raise Exception("Géométrie invalide")
        
        # Diamètre équivalent (cercle de même surface)
        equivalent_diameter = 2 * np.sqrt(area / np.pi) if HAS_NUMPY else 2 * (area / 3.14159) ** 0.5
        
        # Ratio profondeur/diamètre (dolines typiques: 0.1-0.3)
        depth_diameter_ratio = diff_alt / equivalent_diameter if equivalent_diameter > 0 else 0
        
        # Circularité pondérée par la profondeur
        basic_circularity = (4 * 3.14159 * area) / (perimeter ** 2)
        depth_weight = min(diff_alt / 2.0, 1.0)  # Poids basé sur profondeur, max 1.0
        circularity_weighted = basic_circularity * (0.5 + 0.5 * depth_weight)
        
        # Ratio volume théorique / surface
        # Volume approximé d'un cône tronqué
        theoretical_volume = (area * diff_alt) / 3.0  # Approximation conique
        volume_surface_ratio = theoretical_volume / area if area > 0 else 0
        
        # Indice de compacité modifié (forme optimale pour drainage)
        compactness_modified = area / (perimeter ** 2) * (1 + diff_alt/10.0)
        
        # Estimation de la régularité du contour
        # Ratio périmètre réel / périmètre cercle équivalent
        circle_perimeter = 2 * 3.14159 * (equivalent_diameter / 2)
        contour_regularity = circle_perimeter / perimeter if perimeter > 0 else 0
        
        # Indice d'asymétrie approximé (basé sur la circularité)
        # Plus proche de 1 = plus symétrique
        asymmetry_index = min(basic_circularity * 2, 1.0)
        
        return {
            'depth_diameter_ratio': float(depth_diameter_ratio),
            'circularity_weighted': float(circularity_weighted),
            'volume_surface_ratio': float(volume_surface_ratio),
            'compactness_modified': float(compactness_modified),
            'contour_regularity': float(contour_regularity),
            'asymmetry_index': float(asymmetry_index)
        }
        
    except Exception as e:
        logging.warning(f"Erreur calcul morphométrie karstique: {str(e)}")
        return {
            'depth_diameter_ratio': 0, 'circularity_weighted': 0, 'volume_surface_ratio': 0,
            'compactness_modified': 0, 'contour_regularity': 0, 'asymmetry_index': 0
        }


def calculate_plateau_calcaire_features(depression_feature, mnt_path):
    """Calcule toutes les features spécialisées plateau calcaire"""
    
    # Récupérer géométrie et attributs
    geom = depression_feature.geometry()
    attributes = {
        'Area': safe_get_attribute(depression_feature, 'Area', 0),
        'Perimeter': safe_get_attribute(depression_feature, 'Perimeter', 0),
        'DiffAlt': safe_get_attribute(depression_feature, 'DiffAlt', 0),
        'Altitude': safe_get_attribute(depression_feature, 'Altitude', 0)
    }
    
    # Features anti-buttes (contexte topographique)
    elevation_features = calculate_elevation_context_features(geom, mnt_path)
    
    # Features morphométrie karstique
    morpho_features = calculate_karst_morphometry_features(geom, attributes)
    
    # Features classiques optimisées
    area = float(attributes['Area'])
    diff_alt = float(attributes['DiffAlt'])
    
    classic_features = {
        'area_log': np.log1p(area) if HAS_NUMPY else np.log(1 + area) if area > 0 else 0,
        'diff_alt_normalized': diff_alt / (area ** 0.5) if area > 0 else 0
    }
    
    # Combiner toutes les features
    all_features = {**elevation_features, **morpho_features, **classic_features}
    
    # Validation et nettoyage
    for key, value in all_features.items():
        if np.isnan(value) or np.isinf(value):
            all_features[key] = 0.0
    
    return all_features


def calculate_advanced_features(features_data):
    """Calcule des features avancées pour le ML plateau calcaire"""
    if not HAS_PANDAS or not HAS_NUMPY:
        logging.warning("Pandas/NumPy manquants - Feature engineering limité")
        return features_data
    
    df = features_data.copy()
    
    # Features géométriques de base
    df['Circularity'] = (4 * np.pi * df['Area']) / (df['Perimeter'] ** 2)
    df['Compactness'] = df['Area'] / (df['Perimeter'] ** 2)
    df['Area_Perimeter_Ratio'] = df['Area'] / (df['Perimeter'] + 1e-6)
    
    # Features de taille
    df['Log_Area'] = np.log1p(df['Area'])
    df['Log_Perimeter'] = np.log1p(df['Perimeter'])
    df['Equiv_Radius'] = np.sqrt(df['Area'] / np.pi)
    
    # Features morphométriques plateau calcaire
    df['Elongation_Index'] = df['Perimeter'] / (2 * np.sqrt(np.pi * df['Area']) + 1e-6)
    df['Irregularity_Index'] = (df['Perimeter'] ** 2) / (4 * np.pi * df['Area'] + 1e-6)
    
    # Features altitudinales spécialisées
    if 'DiffAlt' in df.columns:
        df['Alt_Range_Norm'] = df['DiffAlt'] / (df['Equiv_Radius'] + 1e-6)
        # Feature spéciale plateau calcaire : ratio profondeur/diamètre
        df['Depth_Diameter_Ratio'] = df['DiffAlt'] / (2 * df['Equiv_Radius'] + 1e-6)
        # Circularité pondérée par profondeur
        depth_weight = np.minimum(df['DiffAlt'] / 2.0, 1.0)
        df['Circularity_Weighted'] = df['Circularity'] * (0.5 + 0.5 * depth_weight)
    
    # Catégorisation par taille
    try:
        q25, q50, q75 = df['Area'].quantile([0.25, 0.5, 0.75])
        bins = [0, q25, q50, q75, float('inf')]
        df['Size_Category'] = pd.cut(df['Area'], 
                                    bins=bins, 
                                    labels=[0, 1, 2, 3],
                                    duplicates='drop')
        df['Size_Category'] = df['Size_Category'].astype(float)
    except Exception as e:
        logging.warning(f"Catégorisation taille échouée: {e}")
        df['Size_Category'] = 1.0
    
    # Gestion des valeurs infinies et NaN
    df = df.replace([np.inf, -np.inf], np.nan)
    numeric_columns = df.select_dtypes(include=[np.number]).columns
    df[numeric_columns] = df[numeric_columns].fillna(df[numeric_columns].median())
    
    return df

def step9_train_ensemble_model(naturelles_files, anthropiques_files, faux_files, output_folder):
    """
    Étape 8 : Entraînement d'un modèle Random Forest multi-classes (3 catégories)
    VERSION CORRIGÉE AVEC MAPPING AUTOMATIQUE DES COLONNES
    
    Classes:
    - 0 = Faux positif
    - 1 = Dépression naturelle supposée
    - 2 = Dépression anthropique supposée
    """
    logging.info("=" * 80)
    logging.info("ÉTAPE 8 : Entraînement modèle Random Forest multi-classes")
    logging.info("=" * 80)
    
    if not HAS_SKLEARN:
        raise Exception("Scikit-learn requis pour l'entraînement ML")
    
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.model_selection import train_test_split, cross_val_score
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import classification_report, confusion_matrix, accuracy_score
    import joblib
    
    # === CHARGEMENT DES DONNÉES ===
    logging.info("Chargement des fichiers d'entraînement...")
    
    def load_csv_files(file_list, label):
        """Charge plusieurs fichiers CSV et assigne un label"""
        all_data = []
        for filepath in file_list:
            try:
                df = pd.read_csv(filepath)
                df['label'] = label
                all_data.append(df)
                logging.info(f"  • {os.path.basename(filepath)}: {len(df)} exemples")
            except Exception as e:
                logging.warning(f"  ⚠️ Impossible de charger {filepath}: {e}")
        
        if not all_data:
            return None
        
        return pd.concat(all_data, ignore_index=True)
    
    # Charger les 3 catégories
    df_naturelles = load_csv_files(naturelles_files, 'naturelle')
    df_faux = load_csv_files(faux_files, 'faux_positif')
    
    if df_naturelles is None or df_faux is None:
        raise Exception("Impossible de charger les données d'entraînement (naturelles ou faux positifs manquants)")
    
    # Charger anthropiques (optionnel)
    if anthropiques_files and len(anthropiques_files) > 0:
        df_anthropiques = load_csv_files(anthropiques_files, 'anthropique')
        has_anthropiques = (df_anthropiques is not None and len(df_anthropiques) > 0)
    else:
        df_anthropiques = None
        has_anthropiques = False
    
    # Fusionner les datasets
    if has_anthropiques:
        df_combined = pd.concat([df_naturelles, df_anthropiques, df_faux], ignore_index=True)
        n_classes = 3
        logging.info(f"\n✅ Mode 3 CLASSES détecté")
    else:
        df_combined = pd.concat([df_naturelles, df_faux], ignore_index=True)
        n_classes = 2
        logging.info(f"\n⚠️ Mode 2 CLASSES (binaire) - Aucune dépression anthropique")
    
    logging.info(f"\nRépartition des classes:")
    logging.info(f"  🟢 Dépressions naturelles: {len(df_naturelles)}")
    if has_anthropiques:
        logging.info(f"  🟡 Dépressions anthropiques: {len(df_anthropiques)}")
    logging.info(f"  🔴 Faux positifs: {len(df_faux)}")
    logging.info(f"  📊 TOTAL: {len(df_combined)} exemples")
    
    # === EXTRACTION DES FEATURES AVEC MAPPING AUTOMATIQUE ===
    logging.info("\n🔍 Détection automatique des colonnes...")
    
    # Afficher les colonnes disponibles
    available_cols = list(df_combined.columns)
    logging.info(f"Colonnes détectées dans les CSV: {available_cols}")
    
    # 🆕 CALCULER LA CIRCULARITÉ si absente
    if 'Circularity' not in df_combined.columns:
        if 'Area' in df_combined.columns and 'Perimeter' in df_combined.columns:
            logging.info("⚙️ Calcul automatique de la circularité...")
            df_combined['Circularity'] = (4 * np.pi * df_combined['Area']) / (df_combined['Perimeter'] ** 2)
            df_combined['Circularity'] = df_combined['Circularity'].fillna(0)
            logging.info("✅ Circularité calculée")
        else:
            logging.warning("⚠️ Impossible de calculer la circularité (Area ou Perimeter manquant)")
    
    # Mapping intelligent : noms CSV → noms standardisés
    column_mapping = {
        # Variations possibles pour Périmètre
        'Perimeter': 'Perimeter',
        'Perimetre': 'Perimeter',
        'perimeter': 'Perimeter',
        
        # Variations possibles pour Aire
        'Area': 'Area',
        'Aire': 'Area',
        'area': 'Area',
        
        # Variations possibles pour Circularité
        'Circularity': 'Circularity',
        'Circularite': 'Circularity',
        'Circ_Index': 'Circularity',
        
        # Variations possibles pour Profondeur
        'DiffAlt': 'Depth',
        'Profondeur': 'Depth',
        'Diff_Alt': 'Depth',
        
        # Variations possibles pour Altitude min
        'AltMin': 'AltMin',
        'Altitude_min': 'AltMin',
        'Altmin': 'AltMin',
        
        # Variations possibles pour Altitude max
        'AltMax': 'AltMax',
        'Altitude_max': 'AltMax',
        'Altmax': 'AltMax',
        
        # Nombre de polygones
        'Num_Polygons': 'Num_Polygons'
    }
    
    # Appliquer le mapping
    df_combined = df_combined.rename(columns=column_mapping)

    # V1.2 : compatibilite descendante - renommer les colonnes courtes V1.1
    # en noms longs V1.2 si presentes dans les CSV
    short_long_rename = {short: long for short, long in LEGACY_SHORT_TO_LONG.items()
                         if short in df_combined.columns and long not in df_combined.columns}
    if short_long_rename:
        df_combined = df_combined.rename(columns=short_long_rename)
        logging.info(f"V1.2 : {len(short_long_rename)} colonnes V1.1 (noms courts) renommees en noms longs V1.2")

    # Colonnes standardisées attendues (AVEC Num_Polygons !)
    required_features = ['Perimeter', 'Area', 'Circularity', 'Depth', 'AltMin', 'AltMax', 'Num_Polygons']
    
    # Vérifier quelles colonnes sont disponibles
    available_features = [col for col in required_features if col in df_combined.columns]
    
    if len(available_features) == 0:
        logging.error("❌ Aucune colonne reconnue après mapping !")
        logging.error(f"Colonnes dans le CSV: {list(df_combined.columns)}")
        logging.error(f"Colonnes attendues: {required_features}")
        raise Exception(f"Mapping échoué. Vérifiez les noms de colonnes dans vos CSV.")
    
    logging.info(f"✅ Features mappées et disponibles: {available_features}")

    # =========================================================================
    # V1.1 : Intégration des features BD TOPO
    # =========================================================================
    bdtopo_features_used = []
    bdtopo_onehot_cols = []

    # Features numériques BD TOPO présentes dans les CSV
    for col in BDTOPO_NUMERIC_SHORT:
        if col in df_combined.columns:
            # Convertir en numérique et remplacer les NaN par la médiane
            df_combined[col] = pd.to_numeric(df_combined[col], errors='coerce')
            if df_combined[col].notna().any():
                median_val = df_combined[col].median()
                df_combined[col] = df_combined[col].fillna(median_val)
            else:
                df_combined[col] = 0
            available_features.append(col)
            bdtopo_features_used.append(col)

    # Features catégorielles BD TOPO présentes dans les CSV (one-hot encoding)
    for col in BDTOPO_CATEGORICAL_SHORT:
        if col in df_combined.columns:
            df_combined[col] = df_combined[col].fillna('Inconnu').astype(str)
            dummies = pd.get_dummies(df_combined[col], prefix=col, dummy_na=False)
            df_combined = pd.concat([df_combined, dummies], axis=1)
            available_features.extend(dummies.columns.tolist())
            bdtopo_onehot_cols.extend(dummies.columns.tolist())
            bdtopo_features_used.append(f"{col} (one-hot: {len(dummies.columns)} cols)")

    if bdtopo_features_used:
        logging.info(f"✅ Features BD TOPO ajoutées: {len(bdtopo_features_used)}")
        for feat in bdtopo_features_used:
            logging.info(f"   + {feat}")
    else:
        logging.info("ℹ️ Aucune feature BD TOPO détectée dans les CSV (mode V1.0 compatible)")

    X = df_combined[available_features].values

    # V1.1 : forcer X en float (les booleens one-hot posent probleme a np.isnan)
    try:
        X = X.astype(float)
    except Exception as e:
        logging.warning(f"Conversion X en float partielle : {e}")
        # Conversion forcee element par element
        X = np.array(X, dtype=float)

    # Encoder les labels
    label_mapping = {
        'faux_positif': 0,
        'naturelle': 1,
        'anthropique': 2
    }
    
    y = df_combined['label'].map(label_mapping).values
    
    # Vérifier qu'il n'y a pas de NaN
    if np.isnan(X).any() or np.isnan(y).any():
        logging.warning("⚠️ Valeurs manquantes détectées - remplissage par la médiane")
        X = np.nan_to_num(X, nan=np.nanmedian(X))
    
    # === STANDARDISATION ===
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    
    # === SPLIT TRAIN/TEST ===
    X_train, X_test, y_train, y_test = train_test_split(
        X_scaled, y, 
        test_size=0.3, 
        random_state=42,
        stratify=y
    )
    
    logging.info(f"\nÉchantillons d'entraînement: {len(X_train)}")
    logging.info(f"Échantillons de test: {len(X_test)}")
    
    # === ENTRAÎNEMENT RANDOM FOREST ===
    logging.info("\n🔄 Entraînement du Random Forest...")
    
    rf_model = RandomForestClassifier(
        n_estimators=500,
        max_depth=None,
        min_samples_split=5,
        min_samples_leaf=2,
        max_features='sqrt',
        random_state=42,
        n_jobs=-1,
        class_weight='balanced',  # Important pour classes déséquilibrées
        verbose=0
    )
    
    rf_model.fit(X_train, y_train)
    
    logging.info("✅ Entraînement terminé")
    
    # === VALIDATION CROISÉE ===
    logging.info("\n🔄 Validation croisée (5-fold)...")
    cv_scores = cross_val_score(rf_model, X_train, y_train, cv=5, scoring='accuracy')
    logging.info(f"Scores CV: {cv_scores}")
    logging.info(f"Accuracy moyenne CV: {cv_scores.mean():.3f} (+/- {cv_scores.std():.3f})")
    
    # === ÉVALUATION SUR TEST SET ===
    logging.info("\n📊 Évaluation sur ensemble de test...")
    
    y_pred = rf_model.predict(X_test)
    y_pred_proba = rf_model.predict_proba(X_test)
    
    accuracy = accuracy_score(y_test, y_pred)
    logging.info(f"Accuracy test: {accuracy:.3f}")
    
    # Rapport de classification détaillé
    if has_anthropiques:
        target_names = ['Faux positif', 'Naturelle', 'Anthropique']
    else:
        target_names = ['Faux positif', 'Naturelle']
    
    class_report = classification_report(y_test, y_pred, target_names=target_names)
    logging.info("\nRapport de classification:\n" + class_report)
    
    # Matrice de confusion
    conf_matrix = confusion_matrix(y_test, y_pred)
    logging.info("\nMatrice de confusion:")
    logging.info(conf_matrix)
    
    # Importance des features
    feature_importance = pd.DataFrame({
        'feature': available_features,
        'importance': rf_model.feature_importances_
    }).sort_values('importance', ascending=False)
    
    logging.info("\nImportance des features:")
    for idx, row in feature_importance.iterrows():
        logging.info(f"  {row['feature']}: {row['importance']:.4f}")
    
    # === SAUVEGARDE DU MODÈLE ===
    logging.info("\n💾 Sauvegarde du modèle...")
    
    model_path = os.path.join(output_folder, 'modele_rf.joblib')
    scaler_path = os.path.join(output_folder, 'normaliseur.joblib')
    features_path = os.path.join(output_folder, 'variables.joblib')
    mapping_path = os.path.join(output_folder, 'classes.json')
    
    joblib.dump(rf_model, model_path)
    joblib.dump(scaler, scaler_path)
    joblib.dump(available_features, features_path)
    
    # Sauvegarder le mapping des labels
    import json
    with open(mapping_path, 'w') as f:
        json.dump({
            'label_to_int': label_mapping,
            'int_to_label': {v: k for k, v in label_mapping.items()},
            'n_classes': n_classes,
            'has_anthropiques': has_anthropiques
        }, f, indent=2)
    
    logging.info(f"✅ Modèle sauvegardé: {model_path}")
    logging.info(f"✅ Scaler sauvegardé: {scaler_path}")
    logging.info(f"✅ Features sauvegardées: {features_path}")
    logging.info(f"✅ Mapping sauvegardé: {mapping_path}")

    # === V1.2 : METADATA DU MODELE (pour dialogue info a l'inference) ===
    metadata_path = os.path.join(output_folder, 'modele_metadata.json')

    # Detection du niveau d'utilisation BD TOPO au training
    # Une ligne "a BD TOPO" si TOUTES les colonnes BD TOPO numeriques sont
    # renseignees (non nulles, non vides)
    bdtopo_num_cols_present = [c for c in BDTOPO_NUMERIC_SHORT if c in df_combined.columns]
    bdtopo_cat_cols_present = [c for c in BDTOPO_CATEGORICAL_SHORT if c in df_combined.columns]
    bdtopo_cols_present = bdtopo_num_cols_present + bdtopo_cat_cols_present

    nb_samples_total = len(df_combined)
    nb_samples_with_bdtopo = 0

    if bdtopo_cols_present:
        # Une ligne est "complete BD TOPO" si aucune colonne BD TOPO n'est NaN/vide
        mask_complete = df_combined[bdtopo_cols_present].notna().all(axis=1)
        # Pour les colonnes categorielles, verifier aussi qu'on n'a pas de chaine vide
        for col in bdtopo_cat_cols_present:
            mask_complete = mask_complete & (df_combined[col].astype(str).str.strip() != '')
        nb_samples_with_bdtopo = int(mask_complete.sum())

    if nb_samples_with_bdtopo == 0:
        bdtopo_usage = "none"
    elif nb_samples_with_bdtopo == nb_samples_total:
        bdtopo_usage = "full"
    else:
        bdtopo_usage = "partial"

    metadata = {
        "version": "1.2",
        "date_entrainement": pd.Timestamp.now().isoformat(),
        "bdtopo_usage": bdtopo_usage,
        "nb_samples_total": nb_samples_total,
        "nb_samples_with_bdtopo": nb_samples_with_bdtopo,
        "features_bdtopo_count": len(bdtopo_cols_present),
        "features_geomorpho_count": len([f for f in available_features if f in
                                          ['Perimeter', 'Area', 'Circularity', 'Depth',
                                           'AltMin', 'AltMax', 'Num_Polygons']]),
        "accuracy_test": float(accuracy),
        "accuracy_cv_mean": float(cv_scores.mean()),
        "accuracy_cv_std": float(cv_scores.std())
    }

    with open(metadata_path, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)

    logging.info(f"✅ Metadata sauvegardées: {metadata_path}")
    logging.info(f"   BD TOPO usage : {bdtopo_usage} ({nb_samples_with_bdtopo}/{nb_samples_total} echantillons)")
    
    # === RAPPORT FINAL ===
    report_path = os.path.join(output_folder, 'rapport_entrainement.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write("=" * 80 + "\n")
        f.write("RAPPORT D'ENTRAÎNEMENT - MODÈLE RANDOM FOREST MULTI-CLASSES\n")
        f.write("=" * 80 + "\n\n")
        f.write(f"Date: {pd.Timestamp.now()}\n")
        f.write(f"Nombre de classes: {n_classes}\n\n")
        f.write(f"Répartition des données:\n")
        f.write(f"  🟢 Naturelles: {len(df_naturelles)}\n")
        if has_anthropiques:
            f.write(f"  🟡 Anthropiques: {len(df_anthropiques)}\n")
        f.write(f"  🔴 Faux positifs: {len(df_faux)}\n")
        f.write(f"  📊 Total: {len(df_combined)}\n\n")
        f.write(f"Features utilisées: {available_features}\n\n")
        f.write(f"Hyperparamètres:\n")
        f.write(f"  - n_estimators: 500\n")
        f.write(f"  - max_depth: None\n")
        f.write(f"  - min_samples_split: 5\n")
        f.write(f"  - min_samples_leaf: 2\n")
        f.write(f"  - class_weight: balanced\n\n")
        f.write(f"Performances:\n")
        f.write(f"  - Accuracy CV: {cv_scores.mean():.3f} (+/- {cv_scores.std():.3f})\n")
        f.write(f"  - Accuracy test: {accuracy:.3f}\n\n")
        f.write("Rapport de classification:\n")
        f.write(class_report + "\n\n")
        f.write("Matrice de confusion:\n")
        f.write(str(conf_matrix) + "\n\n")
        f.write("Importance des features:\n")
        f.write(feature_importance.to_string(index=False) + "\n")
    
    logging.info(f"📄 Rapport sauvegardé: {report_path}")
    
    logging.info("=" * 80)
    logging.info("ÉTAPE 8 TERMINÉE - Modèle Random Forest multi-classes entraîné")
    logging.info("=" * 80)
    
    QMessageBox.information(
        None,
        "Entraînement terminé",
        f"Modèle Random Forest entraîné avec succès !\n\n"
        f"Mode: {n_classes} classes\n"
        f"Accuracy test: {accuracy:.1%}\n"
        f"Accuracy CV: {cv_scores.mean():.1%}\n\n"
        f"Fichiers sauvegardés dans:\n{output_folder}"
    )
    
    return model_path



def step10_apply_ml_classification(depressions_layer, model_folder, output_folder, resolution):
    """
    Étape 9 : Application d'un modèle Random Forest multi-classes
    
    Prédiction de 3 classes possibles:
    - 0 = Faux positif (éliminé, pas de couche générée)
    - 1 = Dépression naturelle
    - 2 = Dépression anthropique
    """
    logging.info("=" * 80)
    logging.info("ÉTAPE 9 : Application modèle Random Forest multi-classes")
    logging.info("=" * 80)
    
    if not HAS_SKLEARN:
        raise Exception("Scikit-learn requis pour l'application ML")
    
    import joblib
    import json
    
    # === CHARGEMENT DU MODÈLE ===
    model_path = os.path.join(model_folder, 'modele_rf.joblib')
    scaler_path = os.path.join(model_folder, 'normaliseur.joblib')
    features_path = os.path.join(model_folder, 'variables.joblib')
    mapping_path = os.path.join(model_folder, 'classes.json')
    
    if not os.path.exists(model_path):
        raise Exception(f"Modèle manquant: {model_path}")
    
    logging.info(f"Chargement du modèle: {model_path}")
    rf_model = joblib.load(model_path)
    scaler = joblib.load(scaler_path)
    feature_names = joblib.load(features_path)
    
    # Charger le mapping des labels
    with open(mapping_path, 'r') as f:
        label_info = json.load(f)
    
    n_classes = label_info['n_classes']
    has_anthropiques = label_info['has_anthropiques']
    int_to_label = {int(k): v for k, v in label_info['int_to_label'].items()}
    
    logging.info(f"Modèle chargé: {n_classes} classes")
    logging.info(f"  - Dépressions anthropiques: {'✅ Oui' if has_anthropiques else '❌ Non'}")
    logging.info(f"Features requises: {feature_names}")
    
    # === CHARGEMENT DE LA COUCHE ===
    if isinstance(depressions_layer, str):
        layer = QgsVectorLayer(depressions_layer, "depressions", "ogr")
    else:
        layer = depressions_layer
    
    if not layer.isValid():
        raise Exception("Couche de dépressions invalide")
    
    total_features = layer.featureCount()
    logging.info(f"Dépressions à classifier: {total_features}")
    
    # === EXTRACTION DES FEATURES ===
    logging.info("Extraction des features...")

    # V1.1 : détection des features one-hot BD TOPO + géologie dans feature_names
    # Les colonnes one-hot ont la forme 'bati_nat_Industriel', 'geol_litho_Calcaire', etc.
    onehot_features_map = {}  # {short_col: {one_hot_col: valeur_attendue}}
    all_categorical_cols = BDTOPO_CATEGORICAL_SHORT  # V1.2
    for fname in feature_names:
        for cat_col in all_categorical_cols:
            prefix = f"{cat_col}_"
            if fname.startswith(prefix):
                cat_value = fname[len(prefix):]
                if cat_col not in onehot_features_map:
                    onehot_features_map[cat_col] = {}
                onehot_features_map[cat_col][fname] = cat_value
                break

    if onehot_features_map:
        logging.info(f"V1.2 : {len(onehot_features_map)} features categorielles BD TOPO detectees")

    X_list = []
    features_list = []

    for feature in layer.getFeatures():
        try:
            feature_vector = []
            for fname in feature_names:
                # Cas 1 : feature one-hot BD TOPO
                is_onehot = False
                for cat_col, onehot_dict in onehot_features_map.items():
                    if fname in onehot_dict:
                        # Lire la valeur catégorielle du shapefile
                        raw_val = safe_get_attribute(feature, cat_col, '')
                        expected_val = onehot_dict[fname]
                        feature_vector.append(1.0 if str(raw_val) == expected_val else 0.0)
                        is_onehot = True
                        break

                if is_onehot:
                    continue

                # Cas 2 : feature numérique standard
                value = safe_get_attribute(feature, fname, 0)
                if value is None or value == '':
                    value = 0
                if isinstance(value, str):
                    try:
                        value = float(value.replace(',', '.'))
                    except ValueError:
                        value = 0.0
                try:
                    feature_vector.append(float(value))
                except (TypeError, ValueError):
                    feature_vector.append(0.0)

            X_list.append(feature_vector)
            features_list.append(feature)
        except Exception as e:
            logging.warning(f"Erreur extraction feature {feature.id()}: {e}")
            continue

    if len(X_list) == 0:
        raise Exception("Aucune feature extraite")

    X = np.array(X_list)

    # Gérer les NaN
    if np.isnan(X).any():
        logging.warning("⚠️ Valeurs manquantes détectées - remplissage par 0")
        X = np.nan_to_num(X, nan=0.0)

    # Standardisation
    X_scaled = scaler.transform(X)
    
    # === PRÉDICTION ===
    logging.info("🔄 Classification en cours...")
    
    y_pred = rf_model.predict(X_scaled)
    y_pred_proba = rf_model.predict_proba(X_scaled)
    
    # === SÉPARATION PAR CLASSE ===
    safe_res = to_safe_str(resolution)
    
    naturelles_features = []
    anthropiques_features = []
    faux_count = 0  # On compte juste, pas de couche
    
    for i, (feature, pred_class, probas) in enumerate(zip(features_list, y_pred, y_pred_proba)):
        if pred_class == 0:
            # Faux positif → on l'élimine (juste compter)
            faux_count += 1
            continue
        
        # Créer une copie de la feature pour les vraies dépressions
        new_feature = QgsFeature(feature)
        attrs = list(feature.attributes())
        
        # ML_Class (label textuel)
        class_label = int_to_label.get(int(pred_class), 'unknown')
        attrs.append(class_label)
        
        # ML_Confidence (probabilité de la classe prédite)
        confidence = float(probas[int(pred_class)])
        attrs.append(confidence)
        
        # Probabilités pour chaque classe
        attrs.append(float(probas[0]))  # Proba faux positif
        attrs.append(float(probas[1]))  # Proba naturelle
        if has_anthropiques:
            attrs.append(float(probas[2]))  # Proba anthropique
        
        new_feature.setAttributes(attrs)
        
        # Répartir dans les listes
        if pred_class == 1:
            naturelles_features.append(new_feature)
        elif pred_class == 2:
            anthropiques_features.append(new_feature)
    
    # === STATISTIQUES ===
    logging.info("\n📊 Résultats de la classification:")
    logging.info(f"  🟢 Naturelles : {len(naturelles_features)} ({len(naturelles_features)/total_features*100:.1f}%)")
    if has_anthropiques:
        logging.info(f"  🟡 Anthropiques : {len(anthropiques_features)} ({len(anthropiques_features)/total_features*100:.1f}%)")
    logging.info(f"  🔴 Faux positifs éliminés : {faux_count} ({faux_count/total_features*100:.1f}%)")
    
    # === FONCTION DE SAUVEGARDE ===
    def save_ml_category(features, category_name, color_emoji):
        if len(features) == 0:
            logging.info(f"{color_emoji} Aucune dépression {category_name} prédite")
            return None
        
        # Créer couche avec nouveaux champs ML
        temp_layer = QgsVectorLayer(
            "Polygon?crs=" + layer.crs().authid(),
            category_name,
            "memory"
        )
        provider = temp_layer.dataProvider()
        
        # Copier champs existants
        provider.addAttributes(layer.fields())
        
        # Ajouter champs ML
        provider.addAttributes([
            QgsField("ML_Class", QVariant.String),
            QgsField("ML_Confid", QVariant.Double),
            QgsField("Proba_Faux", QVariant.Double),
            QgsField("Proba_Nat", QVariant.Double)
        ])
        
        if has_anthropiques:
            provider.addAttributes([QgsField("Proba_Anth", QVariant.Double)])
        
        temp_layer.updateFields()
        
        # Ajouter features
        provider.addFeatures(features)
        temp_layer.updateExtents()
        
        # Sauvegarder sur disque
        output_path = os.path.join(
            output_folder,
            f"depressions_ml_{category_name}_{safe_res}m.gpkg"
        )
        
        write_result = QgsVectorFileWriter.writeAsVectorFormat(
            temp_layer,
            output_path,
            "utf-8",
            temp_layer.crs(),
            "GPKG"
        )
        
        if write_result[0] != QgsVectorFileWriter.NoError:
            logging.error(f"Erreur sauvegarde {category_name}: {write_result[1]}")
            return None
        
        # Charger et ajouter au projet QGIS
        final_layer = QgsVectorLayer(output_path, f"depressions_ml_{category_name}_{safe_res}m", "ogr")
        if final_layer.isValid():
            QgsProject.instance().addMapLayer(final_layer)
            logging.info(f"{color_emoji} Couche ajoutée au projet: depressions_ml_{category_name}_{safe_res}m ({len(features)} entités)")
        else:
            logging.error(f"❌ Impossible de charger la couche {category_name}")
        
        return output_path
    
    # === SAUVEGARDE DES COUCHES UTILES UNIQUEMENT ===
    nat_path = save_ml_category(naturelles_features, "naturelles", "🟢")
    
    anth_path = None
    if has_anthropiques and len(anthropiques_features) > 0:
        anth_path = save_ml_category(anthropiques_features, "anthropiques", "🟡")
    
    # Pas de couche pour les faux positifs (c'est le but du ML : les éliminer !)
    
    logging.info("=" * 80)
    logging.info("ÉTAPE 9 TERMINÉE - Classification multi-classes appliquée")
    logging.info("=" * 80)
    
    # === MESSAGE FINAL ===
    total_conservees = len(naturelles_features) + len(anthropiques_features)
    
    msg = f"✅ Classification ML terminée !\n\n"
    msg += f"📊 Résultats :\n"
    msg += f"  🟢 Naturelles : {len(naturelles_features)}\n"
    if has_anthropiques:
        msg += f"  🟡 Anthropiques : {len(anthropiques_features)}\n"
    msg += f"  🔴 Faux positifs éliminés : {faux_count}\n\n"
    msg += f"📈 Taux de filtrage : {faux_count/total_features*100:.1f}%\n"
    msg += f"📈 Dépressions conservées : {total_conservees}/{total_features}\n\n"
    msg += f"📁 Fichiers dans :\n{output_folder}"
    
    QMessageBox.information(None, "Classification ML terminée", msg)
    
    # Retourner le chemin de la couche principale (naturelles)
    return nat_path



# =============================================================================
# Étape 10 : Extraction des points exutoires (Version améliorée)
# =============================================================================

def _read_altitude_at(mnt_band, mnt_ds, x, y, nodata=None):
    """Lit l'altitude du MNT a la coordonnee (x, y) en EPSG:2154.
    Retourne None si hors emprise ou nodata."""
    if mnt_band is None or mnt_ds is None:
        return None
    try:
        from osgeo import gdal  # import local pour eviter import top-level
        gt = mnt_ds.GetGeoTransform()
        j = int((x - gt[0]) / gt[1])
        i = int((gt[3] - y) / abs(gt[5]))
        if j < 0 or j >= mnt_ds.RasterXSize or i < 0 or i >= mnt_ds.RasterYSize:
            return None
        val = mnt_band.ReadAsArray(j, i, 1, 1)
        if val is None:
            return None
        v = float(val[0, 0])
        if nodata is not None and v == nodata:
            return None
        import math as _m
        if _m.isnan(v) or _m.isinf(v):
            return None
        return v
    except Exception:
        return None


def _apply_exutoires_style(layer):
    """Applique un style categorise a la couche exutoires :
    - type_point = 'entree' : triangle bleu
    - type_point = 'point_bas' : cercle rouge"""
    try:
        from qgis.core import (
            QgsMarkerSymbol, QgsSimpleMarkerSymbolLayerBase,
            QgsRendererCategory, QgsCategorizedSymbolRenderer
        )
        from qgis.PyQt.QtGui import QColor

        # Symbole ENTREE : triangle bleu
        sym_entree = QgsMarkerSymbol.createSimple({
            'name': 'triangle',
            'color': '30,144,255',  # Bleu dodger
            'outline_color': '0,0,128',
            'outline_width': '0.4',
            'size': '3.5'
        })

        # Symbole POINT BAS : cercle rouge
        sym_bas = QgsMarkerSymbol.createSimple({
            'name': 'circle',
            'color': '220,20,60',  # Rouge crimson
            'outline_color': '80,0,0',
            'outline_width': '0.3',
            'size': '2.5'
        })

        categories = [
            QgsRendererCategory('entree', sym_entree, 'Point d\'entrée (amont BV)'),
            QgsRendererCategory('point_bas', sym_bas, 'Point bas (fond dépression)')
        ]
        renderer = QgsCategorizedSymbolRenderer('type_point', categories)
        layer.setRenderer(renderer)
        layer.triggerRepaint()
    except Exception as e:
        import logging
        logging.warning(f"Style exutoires non applique : {e}")


def step11_extract_outlet_points(output_folder, resolution, mnt_path, depressions_layer, threshold):
    import os, logging, numpy as np, processing
    from osgeo import gdal
    from qgis.core import (
        QgsVectorLayer, QgsFields, QgsField, QgsFeature, QgsGeometry, QgsPointXY,
        QgsVectorFileWriter, QgsCoordinateReferenceSystem, QgsProject, QgsRectangle,
        edit
    )
    from PyQt5.QtCore import QVariant, QEventLoop, Qt
    from qgis.PyQt.QtWidgets import QMessageBox, QDialog, QVBoxLayout, QLabel, QPushButton

    try:
        logging.info("Début de l'étape 10 : Extraction des points exutoires")

        # Définition des chemins
        depressions_shp = os.path.join(output_folder, f"depressions_{resolution}m.gpkg")
        flow_accum_path = os.path.join(output_folder, "accumulation.tif")
        drainage_path = os.path.join(output_folder, "ecoulement.tif")
        stream_segments_path = os.path.join(output_folder, "reseau_hydro.tif")
        basins_path = os.path.join(output_folder, "bassins_raster.tif")
        output_pts = os.path.join(output_folder, f"exutoires_{resolution}m.gpkg")

        # Charger la couche de dépressions
        if isinstance(depressions_layer, str):
            depressions_layer = QgsVectorLayer(depressions_shp, "Depressions", "ogr")
        
        if not depressions_layer.isValid():
            raise Exception(f"Couche des dépressions non valide : {depressions_shp}")

        # Vérifier si les rasters hydrologiques existent déjà
        required_rasters = [flow_accum_path, drainage_path, stream_segments_path, basins_path]
        if not all(os.path.exists(raster) for raster in required_rasters):
            # Génération des rasters via r.watershed
            params_watershed = {
                'elevation': mnt_path,
                'accumulation': flow_accum_path,
                'drainage': drainage_path,
                'stream': stream_segments_path,
                'basin': basins_path,
                'threshold': threshold,
                'GRASS_REGION_PARAMETER': None,
                'GRASS_REGION_CELLSIZE_PARAMETER': 1,
                'overwrite': True
            }
            processing.run("grass7:r.watershed", params_watershed)
            logging.info("Rasters générés : Flow Accumulation, Drainage Direction, Stream Segments et Basins.")
        else:
            logging.info("Les rasters hydrologiques existent déjà. Réutilisation des fichiers existants.")

        # Création de la couche mémoire pour les points exutoires
        # V1.2 : 2 points par dépression (entrée + point_bas)
        fields = QgsFields()
        fields.append(QgsField('Numero', QVariant.String))
        fields.append(QgsField('type_point', QVariant.String, len=20))  # "entree" | "point_bas"
        fields.append(QgsField('X', QVariant.Double))
        fields.append(QgsField('Y', QVariant.Double))
        fields.append(QgsField('Flow_Accum', QVariant.Double))  # NULL pour point_bas
        fields.append(QgsField('Altitude', QVariant.Double))

        crs = QgsCoordinateReferenceSystem('EPSG:2154')
        pts_layer = QgsVectorLayer('Point?crs=EPSG:2154', 'exutoires', 'memory')
        provider = pts_layer.dataProvider()
        provider.addAttributes(fields)
        pts_layer.updateFields()

        # Regrouper les dépressions par numéro
        depression_groups = {}
        for feature in depressions_layer.getFeatures():
            numero = safe_get_attribute(feature, 'Numero', str(feature.id()))
            if numero not in depression_groups:
                depression_groups[numero] = []
            depression_groups[numero].append(feature)

        # V1.2 : ouvrir aussi le MNT pour lire les altitudes
        mnt_ds = gdal.Open(mnt_path)
        mnt_band = mnt_ds.GetRasterBand(1) if mnt_ds else None
        mnt_nodata = mnt_band.GetNoDataValue() if mnt_band else None

        # Extraction des points exutoires pour chaque groupe de dépressions
        nb_depressions = len(depression_groups)
        logging.info(f"V1.2 : extraction de 2 points par depression ({nb_depressions} depressions)")

        for numero, features in depression_groups.items():
            try:
                # =========================================================
                # PREPARATION : géométrie unifiée de la depression
                # =========================================================
                combined_geom = None
                for feature in features:
                    geom = feature.geometry()
                    if geom and not geom.isEmpty():
                        if combined_geom is None:
                            combined_geom = QgsGeometry(geom)
                        else:
                            combined_geom = combined_geom.combine(geom)

                if combined_geom is None or combined_geom.isEmpty():
                    logging.warning(f"Geometrie vide pour depression {numero}, passage au suivant")
                    continue

                combined_bbox = combined_geom.boundingBox()

                # ==============================================================
                # POINT 1 : POINT D'ENTREE = max flow_accum sur contour exterieur
                # ==============================================================
                # Methode : buffer de 1 pixel MNT autour de la depression,
                # soustraction de la depression -> anneau 1 pixel,
                # on cherche le max flow_accum dans cet anneau.
                pixel_size = float(resolution)  # resolution du MNT en metres

                buffered = combined_geom.buffer(pixel_size, 5)
                ring_geom = buffered.difference(combined_geom)  # anneau exterieur

                # Bbox elargie pour le clip raster
                ring_bbox = ring_geom.boundingBox()
                if ring_bbox.isEmpty():
                    ring_bbox = combined_bbox

                # Clip du raster flow_accum sur la bbox elargie
                clipped_fa = os.path.join(output_folder, f"temp_clip_fa_{numero}.tif")
                processing.run("gdal:cliprasterbyextent", {
                    'INPUT': flow_accum_path,
                    'PROJWIN': (f"{ring_bbox.xMinimum()},{ring_bbox.xMaximum()},"
                                f"{ring_bbox.yMaximum()},{ring_bbox.yMinimum()}"),
                    'OUTPUT': clipped_fa
                })

                ds = gdal.Open(clipped_fa)
                if ds is None:
                    logging.warning(f"Clip flow_accum echec pour depression {numero}")
                    continue

                band = ds.GetRasterBand(1)
                arr = band.ReadAsArray()
                gt = ds.GetGeoTransform()

                if arr is None or arr.size == 0:
                    ds = None
                    try: os.remove(clipped_fa)
                    except: pass
                    continue

                # Parcours des pixels pour trouver max flow_accum DANS L'ANNEAU
                entree_max_fa = -float('inf')
                entree_x, entree_y = None, None

                ny, nx = arr.shape
                for i in range(ny):
                    for j in range(nx):
                        val = arr[i, j]
                        if np.isnan(val) or np.isinf(val):
                            continue
                        # Coord du centre du pixel
                        px = gt[0] + (j + 0.5) * gt[1]
                        py = gt[3] + (i + 0.5) * gt[5]
                        pt = QgsGeometry.fromPointXY(QgsPointXY(px, py))
                        # Doit etre dans l'anneau (hors de la depression, dans le buffer)
                        if ring_geom.contains(pt) and val > entree_max_fa:
                            entree_max_fa = val
                            entree_x, entree_y = px, py

                ds = None
                try: os.remove(clipped_fa)
                except: pass

                # Fallback si anneau vide : prendre max flow_accum dans bbox
                # (dépression isolée ou cas limite)
                if entree_x is None:
                    logging.info(f"Depression {numero} : anneau vide, fallback bbox max")
                    ds = gdal.Open(flow_accum_path)
                    if ds is not None:
                        band = ds.GetRasterBand(1)
                        # lire la bbox complete de la depression
                        gt2 = ds.GetGeoTransform()
                        j_min = int((combined_bbox.xMinimum() - gt2[0]) / gt2[1])
                        j_max = int((combined_bbox.xMaximum() - gt2[0]) / gt2[1]) + 1
                        i_min = int((gt2[3] - combined_bbox.yMaximum()) / abs(gt2[5]))
                        i_max = int((gt2[3] - combined_bbox.yMinimum()) / abs(gt2[5])) + 1
                        j_min, j_max = max(0, j_min), min(ds.RasterXSize, j_max)
                        i_min, i_max = max(0, i_min), min(ds.RasterYSize, i_max)
                        if j_max > j_min and i_max > i_min:
                            arr2 = band.ReadAsArray(j_min, i_min, j_max - j_min, i_max - i_min)
                            if arr2 is not None and arr2.size > 0:
                                try:
                                    max_val = np.nanmax(arr2)
                                    y_idx, x_idx = np.where(arr2 == max_val)
                                    if len(y_idx) > 0:
                                        entree_max_fa = float(max_val)
                                        entree_x = gt2[0] + (j_min + x_idx[0] + 0.5) * gt2[1]
                                        entree_y = gt2[3] + (i_min + y_idx[0] + 0.5) * gt2[5]
                                except Exception:
                                    pass
                        ds = None

                # Ajouter le point d'entree si trouve
                if entree_x is not None:
                    # Altitude au point d'entree
                    alt_entree = _read_altitude_at(mnt_band, mnt_ds, entree_x, entree_y, mnt_nodata)
                    new_feat = QgsFeature(fields)
                    new_feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(entree_x, entree_y)))
                    new_feat.setAttribute('Numero', numero)
                    new_feat.setAttribute('type_point', 'entree')
                    new_feat.setAttribute('X', entree_x)
                    new_feat.setAttribute('Y', entree_y)
                    new_feat.setAttribute('Flow_Accum', float(entree_max_fa))
                    new_feat.setAttribute('Altitude', alt_entree if alt_entree is not None else None)
                    provider.addFeature(new_feat)

                # ==============================================================
                # POINT 2 : POINT BAS = min altitude DANS la depression
                # ==============================================================
                # On cherche le pixel de plus basse altitude a l'interieur
                # du polygone (vrai fond de la dépression).
                if mnt_ds is not None:
                    gt_mnt = mnt_ds.GetGeoTransform()
                    j_min = int((combined_bbox.xMinimum() - gt_mnt[0]) / gt_mnt[1])
                    j_max = int((combined_bbox.xMaximum() - gt_mnt[0]) / gt_mnt[1]) + 1
                    i_min = int((gt_mnt[3] - combined_bbox.yMaximum()) / abs(gt_mnt[5]))
                    i_max = int((gt_mnt[3] - combined_bbox.yMinimum()) / abs(gt_mnt[5])) + 1
                    j_min = max(0, j_min)
                    j_max = min(mnt_ds.RasterXSize, j_max)
                    i_min = max(0, i_min)
                    i_max = min(mnt_ds.RasterYSize, i_max)

                    if j_max > j_min and i_max > i_min:
                        arr_mnt = mnt_band.ReadAsArray(j_min, i_min,
                                                      j_max - j_min, i_max - i_min)
                        bas_min_alt = float('inf')
                        bas_x, bas_y = None, None
                        if arr_mnt is not None:
                            ny_m, nx_m = arr_mnt.shape
                            for ii in range(ny_m):
                                for jj in range(nx_m):
                                    val = arr_mnt[ii, jj]
                                    if np.isnan(val) or np.isinf(val):
                                        continue
                                    if mnt_nodata is not None and val == mnt_nodata:
                                        continue
                                    px = gt_mnt[0] + (j_min + jj + 0.5) * gt_mnt[1]
                                    py = gt_mnt[3] + (i_min + ii + 0.5) * gt_mnt[5]
                                    pt = QgsGeometry.fromPointXY(QgsPointXY(px, py))
                                    # DOIT etre DANS la depression
                                    if combined_geom.contains(pt) and val < bas_min_alt:
                                        bas_min_alt = val
                                        bas_x, bas_y = px, py

                        if bas_x is not None:
                            new_feat = QgsFeature(fields)
                            new_feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(bas_x, bas_y)))
                            new_feat.setAttribute('Numero', numero)
                            new_feat.setAttribute('type_point', 'point_bas')
                            new_feat.setAttribute('X', bas_x)
                            new_feat.setAttribute('Y', bas_y)
                            new_feat.setAttribute('Flow_Accum', None)
                            new_feat.setAttribute('Altitude', float(bas_min_alt))
                            provider.addFeature(new_feat)

                logging.info(f"Depression {numero} : point entree + point bas ajoutes")

            except Exception as e:
                logging.error(f"Erreur traitement depression {numero}: {str(e)}")
                continue

        # Fermer le MNT
        if mnt_ds is not None:
            mnt_ds = None

        # Sauvegarder la couche
        pts_layer.updateExtents()
        write_result = QgsVectorFileWriter.writeAsVectorFormat(
            pts_layer, output_pts, "utf-8", crs, "GPKG"
        )
        
        if write_result[0] != QgsVectorFileWriter.NoError:
            raise Exception(f"Erreur lors de l'écriture : {write_result[1]}")

        # Charger la couche finale
        if os.path.exists(output_pts):
            final_layer = QgsVectorLayer(output_pts, f"exutoires_{resolution}m", "ogr")
            if not final_layer.isValid():
                raise Exception("Impossible de charger la couche finale Pts_Min.")
            # V1.2 : style categorise par type_point (entree bleu, point_bas rouge)
            _apply_exutoires_style(final_layer)
            QgsProject.instance().addMapLayer(final_layer)
            logging.info("Étape 10 validée, couche des points exutoires ajoutée (2 points par depression).")
        else:
            raise Exception("Erreur lors de l'enregistrement du shapefile Pts_Min.")

        # Ajout de la couche Flow Accumulation pour vérification
        flow_accum_layer = QgsRasterLayer(flow_accum_path, "Flow Accumulation")
        if flow_accum_layer.isValid():
            QgsProject.instance().addMapLayer(flow_accum_layer)
            logging.info("La couche Flow Accumulation a été ajoutée pour vérification.")

        # Message conseil pour la symbologie
        QMessageBox.information(None, "Conseil de visualisation",
                                "Deux points ont été générés par dépression :\n"
                                "  • Triangle bleu : point d'ENTREE (utilisé pour le calcul du BV amont)\n"
                                "  • Cercle rouge : point BAS (fond de dépression, informatif)\n\n"
                                "Pour mieux percevoir les contrastes entre pixels, pensez à ajuster la "
                                "symbologie de la couche 'Flow Accumulation' dans QGIS.\n\n"
                                "Vous pourrez ensuite déplacer ou supprimer les points d'entrée si nécessaire.")

        # Modification manuelle par l'utilisateur
        logging.info("Lancement de la modification manuelle des points exutoires par l'utilisateur.")
        wait_for_manual_editing(resolution)
        
        # Mettre à jour les coordonnées après modification manuelle
        update_pts_coordinates(final_layer)
        logging.info("Les champs X et Y ont été mis à jour après modification manuelle.")
        logging.info("✅ Étape 10 entièrement validée.")

        return final_layer

    except Exception as e:
        logging.error(f"❌ Étape 10 non validée : {str(e)}")
        raise


def safe_get_attribute(feature, attr_name, default_value=None):
    """Récupère un attribut de façon sécurisée"""
    try:
        value = feature[attr_name]
        return value if value is not None else default_value
    except (KeyError, TypeError):
        return default_value


def wait_for_manual_editing(resolution):
    """Fenêtre non modale pour modification manuelle"""
    from qgis.PyQt.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton
    from qgis.PyQt.QtCore import QEventLoop, Qt

    dialog = QDialog()
    dialog.setWindowTitle("Modification manuelle requise")
    dialog.setWindowModality(Qt.NonModal)
    dialog.resize(400, 200)
    
    layout = QVBoxLayout()
    label = QLabel(
        f"Vous pouvez maintenant modifier la couche 'exutoires_{resolution}m' "
        "pour corriger manuellement la position des points exutoires.\n\n"
        "La couche 'Flow Accumulation' est affichée pour faciliter la vérification.\n\n"
        "Une fois vos modifications terminées et sauvegardées, cliquez sur 'Continuer'."
    )
    label.setWordWrap(True)
    layout.addWidget(label)
    
    button = QPushButton("Continuer")
    layout.addWidget(button)
    dialog.setLayout(layout)

    loop = QEventLoop()
    button.clicked.connect(loop.quit)
    dialog.show()
    loop.exec_()
    dialog.close()
    logging.info("Validation de l'ajustement manuel effectuée par l'utilisateur.")


def update_pts_coordinates(pts_layer):
    """Met à jour les champs X et Y après modification manuelle"""
    if not pts_layer.isEditable():
        pts_layer.startEditing()
    
    field_names = [field.name() for field in pts_layer.fields()]
    
    try:
        idx_X = field_names.index("X")
        idx_Y = field_names.index("Y")
    except ValueError:
        logging.error("Les champs X et/ou Y n'existent pas dans la couche.")
        return

    # Mettre à jour les coordonnées
    for feat in pts_layer.getFeatures():
        geom = feat.geometry()
        if geom and not geom.isEmpty():
            pt = geom.asPoint()
            new_vals = {idx_X: pt.x(), idx_Y: pt.y()}
            pts_layer.dataProvider().changeAttributeValues({feat.id(): new_vals})
    
    pts_layer.commitChanges()
    logging.info("Coordonnées X et Y mises à jour après modification manuelle.")


# =============================================================================
# Étape 11 : Identification des bassins associés aux dépressions (Version améliorée)
# =============================================================================
def step12_identify_basins(output_folder, resolution, mnt_extent):
    """Étape 11 : Identification des bassins d'alimentation (version robuste)"""
    import os, logging, shutil, processing
    from qgis.core import (
        QgsVectorLayer, QgsField, QgsVectorFileWriter, QgsProject, edit
    )
    from PyQt5.QtCore import QVariant

    try:
        logging.info("Étape 11 : Identification des bassins d'alimentation")

        # Chemins des fichiers
        drainage_path = os.path.join(output_folder, "ecoulement.tif")
        pts_exutoires_path = os.path.join(output_folder, f"exutoires_{resolution}m.gpkg")
        
        # Vérifications préliminaires
        if not os.path.exists(drainage_path):
            raise Exception(f"Fichier drainage direction introuvable : {drainage_path}")
        if not os.path.exists(pts_exutoires_path):
            raise Exception(f"Fichier points exutoires introuvable : {pts_exutoires_path}")

        pts_exutoires_layer = QgsVectorLayer(pts_exutoires_path, "Pts_Min_Depressions", "ogr")
        if not pts_exutoires_layer.isValid():
            raise Exception("Couche des points exutoires non valide")

        # Création du dossier temporaire
        temp_bv_folder = os.path.join(output_folder, "temp_bv")
        os.makedirs(temp_bv_folder, exist_ok=True)
        bv_list = []

        logging.info("Calcul des bassins d'alimentation pour chaque dépression...")

        # V1.2 : filtrer SEULEMENT les points de type 'entree' pour le calcul BV
        # (les 'point_bas' sont informatifs, pas utilises pour BV amont)
        entree_features = []
        for pt_feat in pts_exutoires_layer.getFeatures():
            type_point = safe_get_attribute(pt_feat, 'type_point', 'entree')
            # Retrocompat : si pas de champ type_point (ancien shapefile), on garde
            if type_point == 'entree' or type_point is None:
                entree_features.append(pt_feat)

        logging.info(f"   {len(entree_features)} points d'entree a traiter "
                     f"(sur {pts_exutoires_layer.featureCount()} points au total)")

        # Traitement de chaque point exutoire (uniquement les 'entree')
        for pt_feat in entree_features:
            try:
                num = safe_get_attribute(pt_feat, "Numero", str(pt_feat.id()))
                point = pt_feat.geometry().asPoint()
                outlet_str = f"{point.x()},{point.y()}"

                # Calcul du bassin d'alimentation
                raster_out = os.path.join(temp_bv_folder, f"bv_{num}.tif")
                processing.run("grass7:r.water.outlet", {
                    'input': drainage_path,
                    'coordinates': outlet_str,
                    'output': raster_out,
                    'GRASS_REGION_PARAMETER': f"{mnt_extent[0]},{mnt_extent[1]},{mnt_extent[2]},{mnt_extent[3]}",
                    'GRASS_REGION_CELLSIZE_PARAMETER': 1,
                    'overwrite': True
                })

                # Vérifier que le raster a été créé
                if not os.path.exists(raster_out):
                    logging.warning(f"Bassin non généré pour dépression {num}")
                    continue

                # Polygonisation du bassin
                shp_out = os.path.join(temp_bv_folder, f"bv_{num}.gpkg")
                processing.run("gdal:polygonize", {
                    'INPUT': raster_out,
                    'BAND': 1,
                    'FIELD': 'DN',
                    'EIGHT_CONNECTEDNESS': False,
                    'OUTPUT': shp_out
                })

                if os.path.exists(shp_out):
                    bv_list.append(shp_out)
                    logging.info(f"Bassin d'alimentation calculé pour dépression {num}")
                else:
                    logging.warning(f"Polygonisation échouée pour dépression {num}")

            except Exception as e:
                logging.error(f"Erreur pour dépression {num}: {str(e)}")
                continue

        if not bv_list:
            raise Exception("Aucun bassin d'alimentation n'a pu être généré")

        # Fusion de tous les bassins d'alimentation
        merged_bv = os.path.join(output_folder, f"bassins_{resolution}m.gpkg")
        processing.run("native:mergevectorlayers", {
            'LAYERS': bv_list,
            'CRS': pts_exutoires_layer.crs().authid(),
            'OUTPUT': merged_bv
        })

        # Chargement et amélioration de la couche finale
        bv_layer = QgsVectorLayer(merged_bv, f"bassins_{resolution}m", "ogr")
        if not bv_layer.isValid():
            raise Exception("Couche bassins fusionnée non valide")

        provider = bv_layer.dataProvider()
        
        # Ajouter champ aire si absent
        existing_fields = [f.name() for f in bv_layer.fields()]
        if "Area_BV" not in existing_fields:
            provider.addAttributes([QgsField("Area_BV", QVariant.Double)])
            bv_layer.updateFields()

        # Calcul de l'aire pour chaque bassin
        with edit(bv_layer):
            for feat in bv_layer.getFeatures():
                geom = feat.geometry()
                if geom and not geom.isEmpty():
                    area_m2 = geom.area()
                    feat["Area_BV"] = area_m2
                    bv_layer.updateFeature(feat)

        # Sauvegarder la couche finale
        write_result = QgsVectorFileWriter.writeAsVectorFormat(
            bv_layer, merged_bv, "utf-8", bv_layer.crs(), "GPKG"
        )
        
        if write_result[0] != QgsVectorFileWriter.NoError:
            logging.warning(f"Problème lors de la sauvegarde : {write_result[1]}")
        
        # Ajout à QGIS
        QgsProject.instance().addMapLayer(bv_layer)
        
                
        # Nettoyage des fichiers temporaires
        if os.path.exists(temp_bv_folder):
            try:
                shutil.rmtree(temp_bv_folder)
                logging.info("Fichiers temporaires nettoyés")
            except Exception as e:
                logging.warning(f"Nettoyage partiel des fichiers temporaires : {str(e)}")
        
        # === GÉNÉRATION DU CSV RÉCAPITULATIF ===
        logging.info("📊 Génération du récapitulatif CSV des bassins...")
        
        try:
            csv_path = os.path.join(output_folder, f"synthese_bassins_{resolution}m.csv")
            
            # Collecter les données de chaque bassin
            basin_records = []
            for feat in bv_layer.getFeatures():
                geom = feat.geometry()
                if geom and not geom.isEmpty():
                    area_m2 = geom.area()
                    area_km2 = area_m2 / 1_000_000
                    area_ha = area_m2 / 10_000
                    perimeter_m = geom.length()
                    perimeter_km = perimeter_m / 1000
                    
                    # Indice de compacité (Gravelius)
                    compacity = perimeter_m / (2 * math.sqrt(math.pi * area_m2)) if area_m2 > 0 else 0
                    
                    centroid = geom.centroid().asPoint()
                    dn_value = safe_get_attribute(feat, 'DN', None)
                    
                    basin_records.append({
                        'ID_Bassin': feat.id(),
                        'DN': dn_value,
                        'Surface_m2': round(area_m2, 2),
                        'Surface_ha': round(area_ha, 4),
                        'Surface_km2': round(area_km2, 6),
                        'Perimetre_m': round(perimeter_m, 2),
                        'Perimetre_km': round(perimeter_km, 4),
                        'Indice_Compacite': round(compacity, 3),
                        'Centroide_X': round(centroid.x(), 2),
                        'Centroide_Y': round(centroid.y(), 2)
                    })
            
            if basin_records and HAS_PANDAS:
                df_bassins = pd.DataFrame(basin_records)
                df_bassins.to_csv(csv_path, sep=';', index=False, encoding='utf-8')
                
                logging.info(f"📊 Statistiques des bassins :")
                logging.info(f"   - Nombre : {len(basin_records)}")
                logging.info(f"   - Surface totale : {df_bassins['Surface_km2'].sum():.4f} km²")
                logging.info(f"   - Surface moyenne : {df_bassins['Surface_ha'].mean():.2f} ha")
                logging.info(f"✅ Récapitulatif CSV créé : {csv_path}")
            elif basin_records:
                with open(csv_path, 'w', encoding='utf-8') as f:
                    headers = list(basin_records[0].keys())
                    f.write(';'.join(headers) + '\n')
                    for record in basin_records:
                        f.write(';'.join(str(record[h]) for h in headers) + '\n')
                logging.info(f"✅ Récapitulatif CSV créé : {csv_path}")
                
        except Exception as e:
            logging.warning(f"⚠️ Erreur génération CSV bassins : {str(e)}")
        
        logging.info(f"✅ Étape 11 validée : {merged_bv}")
        logging.info(f"Bassins d'alimentation calculés pour {len(bv_list)} dépressions")
        
        return bv_layer

    except Exception as e:
        logging.error(f"❌ Erreur étape 11: {str(e)}")
        raise

# =============================================================================
# 12. ÉTAPE DE REGROUPEMENT ET CALCUL DE CIRCULARITÉ
# =============================================================================

def step_regroup_and_circularity(output_folder, resolution, depressions_layer):
    """Regroupe les polygones et calcule l'indice de circularité"""
    logging.info("Regroupement des polygones et calcul de l'indice de circularité")
    
    # Chemins
    raw_path = os.path.join(output_folder, f'depressions_{to_safe_str(resolution)}m.gpkg')
    filled_path = os.path.join(output_folder, f'depressions_finales_{to_safe_str(resolution)}m.gpkg')

    def delete_shapefile(path_base):
        """Supprime un GPKG (un seul fichier)."""
        if os.path.exists(path_base):
            try:
                os.remove(path_base)
            except Exception:
                pass

    def regroup_polygons(features, crs_authid, out_shp):
        crs = QgsCoordinateReferenceSystem(crs_authid)
        fields = QgsFields()
        for name, t in [
            ('Numero', QVariant.String),
            ('Perimeter', QVariant.Double),
            ('Area', QVariant.Double),
            ('Altmax', QVariant.Double),
            ('Altmin', QVariant.Double),
            ('DiffAlt', QVariant.Double),
            ('Circ_Index', QVariant.Double),
        ]:
            fields.append(QgsField(name, t))
        
        layer = QgsVectorLayer(f'MultiPolygon?crs={crs.authid()}', 'temp', 'memory')
        provider = layer.dataProvider()
        provider.addAttributes(fields)
        layer.updateFields()
        
        groups = {}
        for f in features:
            groups.setdefault(f['Numero'], []).append(f)
        
        for num, feats in groups.items():
            try:
                alts = [float(f['Altitude']) for f in feats if f['Altitude'] is not None]
                if alts:
                    max_a, min_a = max(alts), min(alts)
                    diff = max_a - min_a
                else:
                    max_a = min_a = diff = 0
                
                geom = QgsGeometry.unaryUnion([f.geometry() for f in feats])
                peri = float(geom.length())
                area = float(geom.area())
                circ = (4 * math.pi * area) / (peri ** 2) if peri > 0 else None
                
                feat = QgsFeature(layer.fields())
                feat.setGeometry(geom)
                feat['Numero'] = num
                feat['Perimeter'] = peri
                feat['Area'] = area
                feat['Altmax'] = max_a
                feat['Altmin'] = min_a
                feat['DiffAlt'] = diff
                feat['Circ_Index'] = circ
                provider.addFeature(feat)
            except Exception as e:
                logging.error('Erreur regroup %s : %s', num, e)
        
        QgsVectorFileWriter.writeAsVectorFormat(layer, out_shp, 'UTF-8', crs, 'GPKG')

    try:
        delete_shapefile(raw_path)
        delete_shapefile(filled_path)

        feats = list(depressions_layer.getFeatures())
        if not feats:
            raise RuntimeError('Aucun polygone à regrouper.')
        
        regroup_polygons(feats, 'EPSG:2154', raw_path)

        # suppression des trous (seuil fixe 2 m²) et création de la couche comblée
        logging.info('Suppression des trous (min_area=2 m²)')
        processing.run(
            'native:deleteholes',
            {
                'INPUT': raw_path,
                'MIN_AREA': 2.0,
                'OUTPUT': filled_path
            }
        )

        # Charger la couche COMBLEE (trous supprimés) dans QGIS
        layer = QgsVectorLayer(filled_path, f'depressions_finales_{to_safe_str(resolution)}m', 'ogr')
        if not layer.isValid():
            raise RuntimeError('Couche finale invalide')
        
        QgsProject.instance().addMapLayer(layer)
        logging.info("✅ Regroupement terminé")
        return layer
        
    except Exception as e:
        logging.error("Erreur regroupement : %s", e)
        return None

# =============================================================================
# 13. TABLEAU DE CONTINGENCE (ÉTAPE OPTIONNELLE)
# =============================================================================

def create_contingency_table(output_folder, resolution):
    """Crée le tableau de contingence avec classification Jenks"""
    logging.info("Création du tableau de contingence")
    
    if not HAS_PANDAS or not HAS_JENKSPY:
        logging.warning("⚠️ Pandas/Jenkspy manquants - Tableau non créé")
        return
    
    try:
        depression_path = os.path.join(output_folder, f'depressions_{to_safe_str(resolution)}m.gpkg')
        depression_layer = QgsVectorLayer(depression_path, 'depressions', 'ogr')
        
        if not depression_layer.isValid():
            raise Exception("Couche de dépressions invalide")
        
        # Collecte des données
        records = []
        for feat in depression_layer.getFeatures():
            records.append({
                'Perimeter': safe_get_attribute(feat, 'Perimeter', 0),
                'Area': safe_get_attribute(feat, 'Area', 0),
                'DiffAlt': safe_get_attribute(feat, 'DiffAlt', 0),
                'Circ_Index': safe_get_attribute(feat, 'Circ_Index', 0)
            })
        
        df = pd.DataFrame(records)
        
        if df.empty:
            raise Exception("Aucune donnée disponible")
        
        # Classification Jenks
        nb_classes = 5
        valid_perimeters = df["Perimeter"].dropna().astype(float)
        
        if len(valid_perimeters) >= nb_classes:
            breaks = jenkspy.jenks_breaks(valid_perimeters, nb_classes)
            
            # Création des classes
            df["Classe"] = pd.cut(
                df["Perimeter"],
                bins=breaks,
                labels=[f"Classe {i+1}" for i in range(nb_classes)],
                include_lowest=True
            )
            
            # Agrégation avec bornes min/max des classes
            summary = df.groupby("Classe").agg(
                Nombre_Depressions=("Perimeter", "size"),
                Perimetre_Min_Classe=("Perimeter", "min"),
                Perimetre_Max_Classe=("Perimeter", "max"),
                Perimeter_Moyenne=("Perimeter", "mean"),
                Area_Moyenne=("Area", "mean"),
                DiffAlt_Moyenne=("DiffAlt", "mean"),
                Circularite_Moyenne=("Circ_Index", "mean")
            ).reset_index()
            
            # Arrondir les valeurs
            summary["Perimetre_Min_Classe"] = summary["Perimetre_Min_Classe"].round(2)
            summary["Perimetre_Max_Classe"] = summary["Perimetre_Max_Classe"].round(2)
            summary["Perimeter_Moyenne"] = summary["Perimeter_Moyenne"].round(2)
            summary["Area_Moyenne"] = summary["Area_Moyenne"].round(2)
            summary["DiffAlt_Moyenne"] = summary["DiffAlt_Moyenne"].round(2)
            summary["Circularite_Moyenne"] = summary["Circularite_Moyenne"].round(3)
            
            # Sauvegarde
            output_csv = os.path.join(output_folder, f"synthese_{to_safe_str(resolution)}m.csv")
            summary.to_csv(output_csv, sep=";", index=False)
            
            logging.info(f"✅ Tableau de contingence créé: {output_csv}")
            logging.info(f"   Bornes Jenks: {[round(b, 2) for b in breaks]}")
        else:
            logging.warning("Pas assez de valeurs pour classification Jenks")
            
    except Exception as e:
        logging.warning(f"⚠️ Tableau de contingence non créé: {str(e)}")

# =============================================================================
# V1.1 : FONCTIONS BD TOPO
# =============================================================================

def init_bdtopo_context(params):
    """
    Initialise le contexte BD TOPO pour DEADKAT V1.2.

    Télécharge les 8 couches BD TOPO via WFS IGN, les ajoute au projet QGIS,
    et construit les index spatiaux pour le calcul des features.

    Parameters
    ----------
    params : dict
        Doit contenir 'use_bdtopo', 'mnt_extent', 'output_folder'.

    Returns
    -------
    tuple (bdtopo_layers, spatial_indexes) ou (None, None) si désactivé/échec
    """
    if not params.get('use_bdtopo', False):
        logging.info("BD TOPO : desactivee par l'utilisateur")
        return None, None

    if not BDTOPO_MODULES_LOADED:
        logging.warning(f"Modules BD TOPO non charges : {BDTOPO_ERROR}")
        return None, None

    bdtopo_layers = None
    spatial_indexes = None

    try:
        logging.info("=" * 80)
        logging.info("BD TOPO : telechargement et ajout au projet")
        logging.info("=" * 80)

        # V1.2 : conversion tuple -> QgsRectangle si besoin
        mnt_extent = params['mnt_extent']
        if isinstance(mnt_extent, tuple):
            if len(mnt_extent) == 4:
                xmin, xmax, ymin, ymax = mnt_extent
                mnt_extent = QgsRectangle(xmin, ymin, xmax, ymax)
                logging.info(f"   Emprise convertie en QgsRectangle : "
                             f"[{xmin:.0f},{ymin:.0f}] - [{xmax:.0f},{ymax:.0f}]")
            else:
                logging.error(f"Tuple mnt_extent invalide : {mnt_extent}")
                return None, None

        bdtopo_layers = load_bdtopo_layers(
            mnt_extent=mnt_extent,
            output_folder=params['output_folder'],
            add_to_project=True,
            buffer_m=500
        )

        # Construction des index spatiaux BD TOPO
        logging.info("Construction des index spatiaux BD TOPO...")
        spatial_indexes = build_spatial_indexes(bdtopo_layers)
        logging.info(f"   {len(spatial_indexes)} index construits")

    except Exception as e:
        logging.error(f"Erreur initialisation BD TOPO : {e}")
        import traceback
        logging.error(traceback.format_exc())

    return bdtopo_layers, spatial_indexes


def enrich_depressions_with_bdtopo_features(depressions_shapefile_path, spatial_indexes, geol_index=None):
    """
    Enrichit un shapefile de dépressions avec les 18 features BD TOPO.

    V1.2 : Optimisation majeure via bulk write (changeAttributeValues)
    au lieu d'appels unitaires (changeAttributeValue). Gain ~10x sur gros
    jeux de donnees (~1000+ polygones).

    Le parametre geol_index est conserve pour signature, mais ignore
    (la geologie a ete retiree de DEADKAT V1.2 - voir header).

    Parameters
    ----------
    depressions_shapefile_path : str
        Chemin vers le shapefile/GPKG a enrichir.
    spatial_indexes : dict or None
        Retour de build_spatial_indexes() pour BD TOPO. Si None, skip complet.
    geol_index : dict or None
        Conserve pour compatibilite d'appel, ignore en V1.2.
    """
    # Skip complet si pas de BD TOPO active
    if spatial_indexes is None:
        return

    if not BDTOPO_MODULES_LOADED:
        return

    if not os.path.exists(depressions_shapefile_path):
        logging.warning(f"   Fichier introuvable : {depressions_shapefile_path}")
        return

    layer = QgsVectorLayer(depressions_shapefile_path, "depressions_to_enrich", "ogr")
    if not layer.isValid():
        logging.warning(f"   Fichier invalide : {depressions_shapefile_path}")
        return

    nb_features = layer.featureCount()
    logging.info(f"Enrichissement BD TOPO : {nb_features} depressions")

    # Features numériques vs catégoriques (BD TOPO)
    numeric_features = {
        'dist_nearest_building_m', 'building_count_100m', 'building_count_500m',
        'dist_nearest_reservoir_m', 'intersects_reservoir',
        'dist_nearest_major_road_m', 'dist_nearest_minor_road_m', 'road_density_200m',
        'dist_nearest_zone_activite_m', 'intersects_zone_activite',
        'dist_nearest_water_surface_m', 'intersects_water_surface',
        'is_in_forest', 'dist_nearest_sport_terrain_m'
    }

    # Ajouter les champs BD TOPO s'ils n'existent pas
    existing_field_names = [f.name() for f in layer.fields()]
    fields_to_add = []

    for long_name, short_name in BDTOPO_FIELD_MAPPING.items():
        if short_name in existing_field_names:
            continue
        if long_name in numeric_features:
            fields_to_add.append(QgsField(short_name, QVariant.Double))
        else:
            fields_to_add.append(QgsField(short_name, QVariant.String, len=50))

    if fields_to_add:
        layer.dataProvider().addAttributes(fields_to_add)
        layer.updateFields()
        logging.info(f"   {len(fields_to_add)} champs BD TOPO ajoutes")

    # V1.2 BULK WRITE + groupement par Numero pour performance 10x
    # Les polygones d'une meme depression (meme Numero) partagent EXACTEMENT
    # le meme contexte anthropique -> on calcule 1 fois par groupe Numero,
    # puis on propage aux autres polygones du meme groupe.

    # Pre-calculer l'index de chaque champ court
    short_to_idx = {}
    for short_name in BDTOPO_FIELD_MAPPING.values():
        idx = layer.fields().indexFromName(short_name)
        if idx >= 0:
            short_to_idx[short_name] = idx

    # Verifier la presence du champ 'Numero' pour le groupement
    numero_idx = layer.fields().indexFromName('Numero')
    use_grouping = (numero_idx >= 0)

    # Etape 1 : indexer les features par Numero (1 seule passe)
    # {numero: [(fid, geom), ...]}
    groups_by_numero = {}
    features_without_numero = []

    for feat in layer.getFeatures():
        if use_grouping:
            numero = feat.attribute('Numero')
            if numero is not None and numero != '':
                groups_by_numero.setdefault(numero, []).append((feat.id(), feat.geometry()))
            else:
                features_without_numero.append((feat.id(), feat.geometry()))
        else:
            features_without_numero.append((feat.id(), feat.geometry()))

    nb_groups = len(groups_by_numero)
    nb_orphans = len(features_without_numero)

    if use_grouping:
        logging.info(f"   Groupement par Numero : {nb_groups} depressions uniques "
                     f"(pour {nb_features} polygones -> gain ~{nb_features/max(nb_groups,1):.1f}x)")
    else:
        logging.info(f"   Pas de champ 'Numero' trouve, calcul par polygone (sans groupement)")

    # Etape 2 : calcul des features 1 fois par groupe Numero
    bulk_updates = {}  # {fid: {field_idx: value, ...}}
    nb_processed = 0
    total_to_compute = nb_groups + nb_orphans
    _t_start_enrich = time.time()
    _next_log_pct = 10  # premier log à 10% pour repérer un blocage tôt

    # 2a) Pour chaque groupe Numero : calculer sur le 1er polygone, propager aux freres
    for idx_group, (numero, polygons) in enumerate(groups_by_numero.items()):
        # Prendre la geometrie du 1er polygone (tous partagent le meme contexte)
        first_fid, first_geom = polygons[0]

        if first_geom is None or first_geom.isEmpty():
            features_values = dict(NA_FEATURES_BDTOPO)
        else:
            features_values = calculate_anthropic_context_features(first_geom, spatial_indexes)

        # Construire le dict d'updates (identique pour tous les polygones du groupe)
        updates = {}
        for long_name, short_name in BDTOPO_FIELD_MAPPING.items():
            idx = short_to_idx.get(short_name)
            if idx is None:
                continue
            val = features_values.get(long_name)
            if val is not None and isinstance(val, float) and math.isnan(val):
                val = None
            updates[idx] = val

        # Propager les updates a TOUS les polygones du groupe
        for fid, _ in polygons:
            bulk_updates[fid] = updates

        nb_processed += 1
        pct = int(nb_processed / total_to_compute * 100)
        if pct >= _next_log_pct:
            elapsed = time.time() - _t_start_enrich
            rate = nb_processed / max(elapsed, 0.001)
            remaining = (total_to_compute - nb_processed) / max(rate, 0.001)
            logging.info(
                f"   Calcul features : {pct}% ({nb_processed}/{total_to_compute}) "
                f"- {elapsed:.1f}s ecoule, ~{remaining:.0f}s restant ({rate:.1f} grp/s)"
            )
            _next_log_pct = pct + 10

    # 2b) Features sans Numero : calcul individuel
    for fid, geom in features_without_numero:
        if geom is None or geom.isEmpty():
            features_values = dict(NA_FEATURES_BDTOPO)
        else:
            features_values = calculate_anthropic_context_features(geom, spatial_indexes)

        updates = {}
        for long_name, short_name in BDTOPO_FIELD_MAPPING.items():
            idx = short_to_idx.get(short_name)
            if idx is None:
                continue
            val = features_values.get(long_name)
            if val is not None and isinstance(val, float) and math.isnan(val):
                val = None
            updates[idx] = val
        bulk_updates[fid] = updates
        nb_processed += 1

    # Etape 3 : ecriture bulk en une seule transaction
    logging.info(f"   Ecriture bulk de {len(bulk_updates)} features...")
    dp = layer.dataProvider()
    success = dp.changeAttributeValues(bulk_updates)

    if success:
        layer.reload()
        logging.info(f"   {nb_features} polygones enrichis ({nb_groups} calculs uniques, bulk)")
    else:
        logging.error("   Echec ecriture bulk BD TOPO")



def main():
    """Fonction principale DEADKAT V1.1 avec BD TOPO"""
    
    errors_log = []
    steps_ok = []
    pr = None
    
    try:
        logging.info("=" * 80)
        logging.info("DEADKAT V1.1 - DEMARRAGE")
        logging.info("=" * 80)
        
        show_requirements()
        steps_ok.append("Prérequis vérifiés")
        
        params = get_user_inputs()
        setup_simple_logging(params['output_folder'])
        
        logging.info(f"Mode sélectionné: {params['mode']}")
        logging.info(f"ML Mode: {params['ml_mode']}")

        # =====================================================================
        # V1.1 : Initialisation BD TOPO (si activée)
        # =====================================================================
        # S'assurer que mnt_extent est bien présent
        if not params.get('mnt_extent') and params.get('mnt_path'):
            try:
                _mnt_layer = QgsRasterLayer(params['mnt_path'], "MNT_tmp")
                if _mnt_layer.isValid():
                    params['mnt_extent'] = _mnt_layer.extent()
            except Exception as e:
                logging.warning(f"Impossible de recuperer l'emprise MNT : {e}")

        bdtopo_layers, spatial_indexes = init_bdtopo_context(params)
        params['bdtopo_layers'] = bdtopo_layers
        params['spatial_indexes'] = spatial_indexes

        if bdtopo_layers is not None:
            steps_ok.append("BD TOPO chargée")
        
        pr = profile_start()
        
        # ===================================================================
        # 🧠 MODE : ENTRAÎNER UN MODÈLE ML
        # ===================================================================
        if params['mode'] == "Entraîner modèle":
            logging.info("🧠 Mode : Entraînement ML")
            
            model_info = step9_train_ensemble_model(
                params.get('naturelles_files'),
                params.get('anthropiques_files'),
                params.get('faux_files'),
                params['output_folder']
            )
            steps_ok.append("Entraînement ML")
            
            logging.info("✅ ENTRAÎNEMENT ML TERMINÉ")
            return
        
        # ===================================================================
        # 🏷️ MODE : INTERFACE ÉTIQUETAGE
        # ===================================================================
        elif params['mode'] == "Interface étiquetage":
            logging.info("🏷️ Mode : Création données d'entraînement")
            
            depressions_training_path = os.path.join(
                params['output_folder'],
                f"depressions_etiquetage_{to_safe_str(params['resolution_value'])}m.gpkg"
            )
            
            if os.path.exists(depressions_training_path):
                logging.info("✅ Fichier training existant trouvé - Chargement direct")

                # V1.1 : enrichir le shapefile existant avec les features BD TOPO
                # si BD TOPO est active et si les colonnes ne sont pas deja presentes
                if params.get('spatial_indexes') is not None:
                    tmp_layer = QgsVectorLayer(depressions_training_path, "tmp_check", "ogr")
                    if tmp_layer.isValid():
                        field_names = [f.name() for f in tmp_layer.fields()]
                        if 'dist_nearest_building_m' not in field_names and 'dist_bati' not in field_names:
                            logging.info("BD TOPO : enrichissement du shapefile training existant...")
                            enrich_depressions_with_bdtopo_features(
                                depressions_training_path,
                                params['spatial_indexes']
                            )
                        else:
                            logging.info("BD TOPO : colonnes deja presentes dans le shapefile training")

                depressions_layer = QgsVectorLayer(
                    depressions_training_path, 
                    f"depressions_etiquetage_{to_safe_str(params['resolution_value'])}m", 
                    "ogr"
                )
                
                if depressions_layer.isValid():
                    dock = step8_create_training_interface(
                        depressions_layer,
                        params['output_folder'],
                        csv_output_folder=params['output_folder'],
                        generate_training_files=True
                    )
                    steps_ok.append("Interface étiquetage (données existantes)")
                    return
                else:
                    logging.warning("⚠️ Fichier training invalide - Régénération")
            
            logging.info("Aucune dépression training trouvée - Exécution détection complète")
            
            # ÉTAPE 1
            interval = step1_extract_contours(
                params['mnt_path'],
                params['output_folder'],
                params['resolution_value']
            )
            steps_ok.append("Étape 1")
            
            # ÉTAPE 2
            step2_polygonize_contours(
                params['output_folder'],
                params['resolution_value'],
                interval
            )
            steps_ok.append("Étape 2")
            
            # ÉTAPE 3
            step3_add_fields_and_attributes(
                params['output_folder'],
                params['resolution_value']
            )
            steps_ok.append("Étape 3")
            
            # ÉTAPE 4
            filtered_layer = step4_filter_polygons(
                params['output_folder'],
                params['resolution_value'],
                params['min_perimeter'],
                params['max_perimeter']
            )
            steps_ok.append("Étape 4")
            
            # ÉTAPE 5
            envelopes_path = step5_identify_envelopes(
                params['output_folder'],
                params['resolution_value']
            )
            steps_ok.append("Étape 5")
            
            # ÉTAPE 6 - Génère directement depressions_verifiees
            depressions_verified_path = step6_identify_depressions(
                params['output_folder'],
                params['resolution_value'],
                filtered_layer,
                params['min_diff'],
                share_threshold=params['share_threshold'],
                position_threshold=params['position_threshold'],  
                strategy_name=params['strategy_name']  
            )
            steps_ok.append("Étape 6")

            # V1.1 : Enrichissement avec features BD TOPO
            enrich_depressions_with_bdtopo_features(
                depressions_verified_path,
                params.get('spatial_indexes')
            )
            
            # PAS DE STEP7 - Plus nécessaire !
            
            # Charger la couche vérifiée
            cuvettes_verified_layer = QgsVectorLayer(
                depressions_verified_path,
                f"depressions_verifiees_{to_safe_str(params['resolution_value'])}m",
                "ogr"
            )
            
            logging.info("ℹ️ Mode training : conservation des polygones détaillés (pas de fusion)")
            
            cleanup_unwanted_training_files(params['output_folder'])
            
            # V1.2 : Pour GPKG, une simple copie du fichier préserve TOUS les champs
            # (alternative a writeAsVectorFormat qui perdait les colonnes BD TOPO).
            # Gain : plus besoin de re-enrichir, facteur temps ~2x.
            logging.info(f"💾 Sauvegarde des dépressions DÉTAILLÉES pour training...")

            import shutil
            try:
                if os.path.exists(depressions_training_path):
                    os.remove(depressions_training_path)
                shutil.copy2(depressions_verified_path, depressions_training_path)
                logging.info(f"✅ Dépressions training copiées (GPKG intact): {os.path.basename(depressions_training_path)}")
            except Exception as e:
                raise Exception(f"❌ Erreur copie GPKG training: {e}")

            final_depressions_layer = QgsVectorLayer(
                depressions_training_path,
                f"depressions_etiquetage_{to_safe_str(params['resolution_value'])}m",
                "ogr"
            )
            
            if not final_depressions_layer.isValid():
                raise Exception("❌ Impossible de recharger la couche training")
            
            logging.info("🧹 Nettoyage des couches intermédiaires du projet QGIS...")
            cleanup_intermediate_layers_from_project(params['resolution_value'], mode="training")
            
            logging.info("🏷️ Lancement interface d'étiquetage avec polygones détaillés...")
            dock = step8_create_training_interface(
                final_depressions_layer,
                params['output_folder'],
                csv_output_folder=params['output_folder'],
                generate_training_files=True
            )
            steps_ok.append("Interface étiquetage")
            return
        
        # ===================================================================
        # 🤖 MODE : APPLIQUER MODÈLE ML (avec ou sans bassins)
        # ===================================================================
        elif "Appliquer modèle" in params['mode']:
            logging.info(f"🤖 Mode : {params['mode']}")
            
            # Phase 1 : Détection (étapes 1-6)
            logging.info("🔍 Phase 1 : Détection")
            
            interval = step1_extract_contours(
                params['mnt_path'],
                params['output_folder'],
                params['resolution_value']
            )
            steps_ok.append("Étape 1")
            
            step2_polygonize_contours(
                params['output_folder'],
                params['resolution_value'],
                interval
            )
            steps_ok.append("Étape 2")
            
            step3_add_fields_and_attributes(
                params['output_folder'],
                params['resolution_value']
            )
            steps_ok.append("Étape 3")
            
            filtered_layer = step4_filter_polygons(
                params['output_folder'],
                params['resolution_value'],
                params['min_perimeter'],
                params['max_perimeter']
            )
            steps_ok.append("Étape 4")
            
            envelopes_path = step5_identify_envelopes(
                params['output_folder'],
                params['resolution_value']
            )
            steps_ok.append("Étape 5")
            
            # ÉTAPE 6 - Génère directement depressions_verifiees
            depressions_verified_path = step6_identify_depressions(
                params['output_folder'],
                params['resolution_value'],
                filtered_layer,
                params['min_diff'],
                share_threshold=params['share_threshold'],
                position_threshold=params['position_threshold'],  
                strategy_name=params['strategy_name']  
            )
            steps_ok.append("Étape 6")

            # V1.1 : Enrichissement avec features BD TOPO
            enrich_depressions_with_bdtopo_features(
                depressions_verified_path,
                params.get('spatial_indexes')
            )
            
            # PAS DE STEP7
            
            # Charger la couche vérifiée
            cuvettes_verified_layer = QgsVectorLayer(
                depressions_verified_path,
                f"depressions_verifiees_{to_safe_str(params['resolution_value'])}m",
                "ogr"
            )
            
            # Regroupement
            final_depressions_layer = step_regroup_and_circularity(
                params['output_folder'],
                params['resolution_value'],
                cuvettes_verified_layer
            )
            steps_ok.append("Regroupement")
            
            cleanup_unwanted_training_files(params['output_folder'])
            
            logging.info("🧹 Nettoyage des couches intermédiaires (mode ML)...")
            cleanup_intermediate_layers_from_project(params['resolution_value'], mode="training")
            
            depressions_path = os.path.join(
                params['output_folder'],
                f"depressions_{to_safe_str(params['resolution_value'])}m.gpkg"
            )
            
            logging.info(f"🔄 Rechargement de la couche depuis le disque: {depressions_path}")
            final_depressions_layer = QgsVectorLayer(
                depressions_path,
                f"depressions_{to_safe_str(params['resolution_value'])}m",
                "ogr"
            )
            
            if not final_depressions_layer.isValid():
                raise Exception(f"❌ Impossible de recharger la couche : {depressions_path}")
            
            logging.info(f"✅ Couche rechargée : {final_depressions_layer.featureCount()} dépressions")
            
            # Phase 2 : Application ML
            logging.info("🤖 Phase 2 : Application ML")
            
            nat_layer_path = step10_apply_ml_classification(
                final_depressions_layer,
                params['ml_model_folder'],
                params['output_folder'],
                params['resolution_value']
            )
            steps_ok.append("Application ML")
            
            # Phase 3 : Bassins (si demandé) - uniquement sur naturelles
            if "Bassins" in params['mode']:
                logging.info("💧 Phase 3 : Hydrologie (sur dépressions naturelles uniquement)")
                
                nat_layer = QgsVectorLayer(nat_layer_path, "depressions_ml_naturelles", "ogr")
                
                step11_extract_outlet_points(
                    params['output_folder'],
                    params['resolution_value'],
                    params['mnt_path'],
                    nat_layer,
                    params['threshold']
                )
                steps_ok.append("Points exutoires")
                
                step12_identify_basins(
                    params['output_folder'],
                    params['resolution_value'],
                    params['mnt_extent']
                )
                steps_ok.append("Bassins")
        
        # ===================================================================
        # 💧 MODE : DÉPRESSIONS + BASSINS CLASSIQUE (sans ML)
        # ===================================================================
        elif params['mode'] == "Dépressions + Bassins classique":
            logging.info("💧 Mode : Dépressions + Bassins classique")
            
            # Phase 1 : Détection (étapes 1-6)
            interval = step1_extract_contours(
                params['mnt_path'],
                params['output_folder'],
                params['resolution_value']
            )
            steps_ok.append("Étape 1")
            
            step2_polygonize_contours(
                params['output_folder'],
                params['resolution_value'],
                interval
            )
            steps_ok.append("Étape 2")
            
            step3_add_fields_and_attributes(
                params['output_folder'],
                params['resolution_value']
            )
            steps_ok.append("Étape 3")
            
            filtered_layer = step4_filter_polygons(
                params['output_folder'],
                params['resolution_value'],
                params['min_perimeter'],
                params['max_perimeter']
            )
            steps_ok.append("Étape 4")
            
            envelopes_path = step5_identify_envelopes(
                params['output_folder'],
                params['resolution_value']
            )
            steps_ok.append("Étape 5")
            
            # ÉTAPE 6 - Génère directement depressions_verifiees
            depressions_verified_path = step6_identify_depressions(
                params['output_folder'],
                params['resolution_value'],
                filtered_layer,
                params['min_diff'],
                share_threshold=params['share_threshold'],
                position_threshold=params['position_threshold'],  
                strategy_name=params['strategy_name']  
            )
            steps_ok.append("Étape 6")

            # V1.1 : Enrichissement avec features BD TOPO
            enrich_depressions_with_bdtopo_features(
                depressions_verified_path,
                params.get('spatial_indexes')
            )
            
            # PAS DE STEP7
            
            # Charger la couche vérifiée
            cuvettes_verified_layer = QgsVectorLayer(
                depressions_verified_path,
                f"depressions_verifiees_{to_safe_str(params['resolution_value'])}m",
                "ogr"
            )
            
            # Phase 2 : Regroupement
            final_depressions_layer = step_regroup_and_circularity(
                params['output_folder'],
                params['resolution_value'],
                cuvettes_verified_layer
            )
            steps_ok.append("Regroupement")
            
            cleanup_unwanted_training_files(params['output_folder'])
            
            # Phase 3 : Hydrologie
            logging.info("💧 Phase 2 : Hydrologie")
            
            step11_extract_outlet_points(
                params['output_folder'],
                params['resolution_value'],
                params['mnt_path'],
                final_depressions_layer,
                params['threshold']
            )
            steps_ok.append("Points exutoires")
            
            step12_identify_basins(
                params['output_folder'],
                params['resolution_value'],
                params['mnt_extent']
            )
            steps_ok.append("Bassins")
        
        # ===================================================================
        # 🔍 MODE : DÉPRESSIONS SEULES
        # ===================================================================
        elif params['mode'] == "Dépressions seules":
            logging.info("🔍 Mode : Dépressions seules")
            
            interval = step1_extract_contours(
                params['mnt_path'],
                params['output_folder'],
                params['resolution_value']
            )
            steps_ok.append("Étape 1")
            
            step2_polygonize_contours(
                params['output_folder'],
                params['resolution_value'],
                interval
            )
            steps_ok.append("Étape 2")
            
            step3_add_fields_and_attributes(
                params['output_folder'],
                params['resolution_value']
            )
            steps_ok.append("Étape 3")
            
            filtered_layer = step4_filter_polygons(
                params['output_folder'],
                params['resolution_value'],
                params['min_perimeter'],
                params['max_perimeter']
            )
            steps_ok.append("Étape 4")
            
            envelopes_path = step5_identify_envelopes(
                params['output_folder'],
                params['resolution_value']
            )
            steps_ok.append("Étape 5")
            
            # ÉTAPE 6 - Génère directement depressions_verifiees
            depressions_verified_path = step6_identify_depressions(
                params['output_folder'],
                params['resolution_value'],
                filtered_layer,
                params['min_diff'],
                share_threshold=params['share_threshold'],
                position_threshold=params['position_threshold'],  
                strategy_name=params['strategy_name']  
            )
            steps_ok.append("Étape 6")

            # V1.1 : Enrichissement avec features BD TOPO
            enrich_depressions_with_bdtopo_features(
                depressions_verified_path,
                params.get('spatial_indexes')
            )
            
            # PAS DE STEP7
            
            # Charger la couche vérifiée
            cuvettes_verified_layer = QgsVectorLayer(
                depressions_verified_path,
                f"depressions_verifiees_{to_safe_str(params['resolution_value'])}m",
                "ogr"
            )
            
            final_depressions_layer = step_regroup_and_circularity(
                params['output_folder'],
                params['resolution_value'],
                cuvettes_verified_layer
            )
            steps_ok.append("Regroupement")
            
            cleanup_unwanted_training_files(params['output_folder'])
        
        # ===================================================================
        # TABLEAU DE CONTINGENCE
        # ===================================================================
        try:
            create_contingency_table(
                params['output_folder'],
                params['resolution_value']
            )
            steps_ok.append("Tableau de contingence")
        except:
            logging.warning("⚠️ Tableau de contingence non créé")
        
        # ===================================================================
        # MESSAGE FINAL
        # ===================================================================
        logging.info("=" * 80)
        logging.info("✅ DEADKAT V1.0 - WORKFLOW TERMINÉ")
        logging.info("=" * 80)
        logging.info(f"Étapes réussies : {len(steps_ok)}")
        
        message = f"✅ Workflow terminé !\n\n"
        message += f"Mode : {params['mode']}\n"
        message += f"Résolution : {params['resolution_value']}m\n"
        message += f"Étapes : {len(steps_ok)}\n\n"
        message += f"📁 Résultats : {params['output_folder']}"
        
        QMessageBox.information(None, "DEADKAT V1.0", message)
    
    except Exception as e:
        logging.error(f"❌ ERREUR CRITIQUE : {str(e)}")
        import traceback
        logging.error(traceback.format_exc())
        
        message = f"❌ Erreur !\n\n{str(e)}\n\n"
        message += f"Étapes OK : {len(steps_ok)}\n"
        message += f"Voir journal.log"
        
        QMessageBox.critical(None, "DEADKAT V1.0 - Erreur", message)
    
    finally:
        if pr and 'params' in locals():
            try:
                profile_save(pr, params['output_folder'])
            except:
                pass
        
        logging.info("🏁 Fin DEADKAT V1.0")

# =============================================================================
# 15. EXÉCUTION DIRECTE
# =============================================================================

# Lancement automatique du script dans QGIS
main()