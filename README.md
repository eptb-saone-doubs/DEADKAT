# DEADKAT : Détection Automatisée des Dépressions KArsTiques

DEADKAT détecte automatiquement les dépressions karstiques fermées (dolines) à partir des MNT LiDAR de l'IGN. Il s'exécute dans QGIS et ne demande aucune programmation. L'outil est développé par le Pôle Karst de l'EPTB Saône et Doubs.

Version 1.0, décembre 2026.

## Ce que fait l'outil

- **Détection géomorphologique (étapes 1 à 6).** Extraction des isolignes fermées du MNT, regroupement en enveloppes candidates, puis six tests qui séparent les dépressions des buttes.
- **Classification (étapes 7 à 9).** Étiquetage assisté, puis forêt aléatoire en trois classes : dépressions naturelles, dépressions anthropiques, faux positifs. Le modèle s'appuie sur la morphométrie et, en option, sur 18 variables de contexte issues de la BD TOPO de l'IGN.
- **Hydrologie (étapes 10 et 11).** Point d'entrée, point bas et bassin d'alimentation de chaque dépression.
- **Exports.** Couches GeoPackage, tableaux CSV et journal d'exécution.

Sur le secteur test de Septfontaines (Doubs, 4 km²), 89 % des 110 détections vérifiées sur le terrain sont confirmées, et l'outil retrouve 81 % des 68 dépressions observées.

## Fichiers

| Fichier | Rôle |
| --- | --- |
| `DEADKAT.py` | Script principal : détection, étiquetage, apprentissage, hydrologie, exports |
| `DEADKAT_bdtopo.py` | Téléchargement des 8 couches BD TOPO par le service WFS de la Géoplateforme IGN |
| `DEADKAT_features_bdtopo.py` | Calcul des 18 variables de contexte anthropique |
| `DEADKAT_installation.py` | Vérification et installation des bibliothèques Python, contrôle de GRASS |
| `docs/Rapport_DEADKAT_v1.0.pdf` | Rapport méthodologique et guide d'utilisation |
| `README.md` | Ce fichier |
| `CHANGELOG.md` | Historique des versions |
| `LICENSE` | Texte de la licence GPL-3.0 |

Les quatre fichiers `.py` doivent se trouver dans un même dossier.

## Prérequis

- **QGIS** 3.16 au minimum, 3.30 ou plus récent recommandé, avec le fournisseur de traitements GRASS activé.
- **Bibliothèques Python :** NumPy et SciPy, en général livrées avec QGIS ; pandas, jenkspy, scikit-learn et joblib, à installer (voir ci-dessous).
- **Connexion internet** pour l'installation et pour la BD TOPO.
- **MNT :** modèle de terrain (et non de surface) en Lambert-93 (EPSG:2154), en mètres, sans valeur manquante sur la zone d'étude. Le MNT LiDAR HD de l'IGN au pas de 0,5 m est recommandé. La version 1.0 a traité 72 km² en moins de 9 minutes pour la détection ; une emprise de 1 000 km² a bloqué.

## Installation

1. Télécharger la dernière version (onglet **Releases**, archive zip) et placer les quatre fichiers `.py` dans un même dossier.
2. Dans QGIS : **Extensions > Console Python**, puis **Afficher l'éditeur** et **Ouvrir un script**. Choisir `DEADKAT_installation.py`, puis cliquer sur **Exécuter le script**.
3. Le script dresse l'état des bibliothèques, propose d'installer celles qui manquent, vérifie GRASS et affiche un bilan. Redémarrer QGIS après l'installation.

En cas d'échec, saisir dans la console Python de QGIS les deux lignes suivantes, puis redémarrer QGIS :

```python
import pip
pip.main(['install', '--user', 'pandas', 'jenkspy', 'scikit-learn', 'joblib'])
```

## Utilisation

Ouvrir `DEADKAT.py` dans l'éditeur de la console Python de QGIS et cliquer sur **Exécuter le script**. L'outil enchaîne les boîtes de dialogue : fenêtre des prérequis, résolution d'analyse, parcours, dossier de sortie, MNT, puis réglages de détection (lissage, remplissage, périmètres, dénivelé minimal, stratégie anti-buttes).

Six parcours sont proposés :

| Parcours | Rôle |
| --- | --- |
| Option 1 | Détection seule (inventaire rapide) |
| Option 2 | Détection et analyse hydrologique |
| ML-1 | Création d'un jeu d'entraînement par étiquetage |
| ML-2 | Entraînement d'un modèle à partir des fichiers d'annotation |
| ML-3a | Application d'un modèle entraîné |
| ML-3b | Application d'un modèle entraîné, puis analyse hydrologique |

Les résultats sont écrits dans le dossier de sortie. Le fichier `journal.log` y retrace chaque traitement et les statistiques de filtrage. L'annexe A du rapport détaille chaque réglage et chaque sortie.

## Principales sorties

| Fichier | Contenu |
| --- | --- |
| `depressions_finales_{rés}m.gpkg` | Dépressions vérifiées et leurs attributs morphométriques |
| `formes_rejetees_{rés}m.gpkg` | Formes écartées, avec le motif du rejet (champ `Raison_Rej`) |
| `depressions_ml_naturelles_{rés}m.gpkg`, `depressions_ml_anthropiques_{rés}m.gpkg` | Dépressions classées par le modèle, avec classe et score de confiance |
| `exutoires_{rés}m.gpkg` | Points d'entrée et points bas |
| `bassins_{rés}m.gpkg`, `synthese_bassins_{rés}m.csv` | Bassins d'alimentation et leur synthèse |
| `synthese_{rés}m.csv` | Tableau de contingence par classe de périmètre |
| `journal.log` | Journal d'exécution |

`{rés}` désigne la résolution d'analyse : `0_5` pour 0,5 m dans la plupart des noms, `0.5` pour les points et les bassins.

## Limites

- DEADKAT ne détecte que les dépressions fermées. Les dépressions ouvertes en versant restent hors de son champ.
- Les sorties sont une aide à la cartographie et demandent une validation experte avant tout usage.
- Les grandes emprises demanderont un mode de tuilage, prévu pour la version 1.1.

## Citer DEADKAT

Fister, V. (2026). *DEADKAT : Détection Automatisée des Dépressions KArsTiques. Développement méthodologique et guide d'utilisation.* Version 1.0. EPTB Saône et Doubs, Pôle Karst. 42 p.

## Licence

Copyright (C) 2026 EPTB Saône et Doubs.

DEADKAT est un logiciel libre, distribué sous licence GNU GPL version 3 ou ultérieure (GPL-3.0-or-later). Voir le fichier `LICENSE`.

L'outil ne redistribue aucune donnée. L'utilisateur télécharge lui-même le MNT et la BD TOPO auprès de l'IGN, sous Licence Ouverte Etalab 2.0.

## Contact

Vincent Fister. Pôle Karst, EPTB Saône et Doubs. Pour signaler un problème ou proposer une amélioration, utiliser l'onglet **Issues** du dépôt.
