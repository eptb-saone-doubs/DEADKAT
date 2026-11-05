# Journal des modifications

Ce fichier recense les changements de chaque version de DEADKAT. Le format s'inspire de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/).

## [1.0] - 2026-12

Première version publique.

### Ajouté

- Détection géomorphologique des dépressions fermées à partir des isolignes du MNT (étapes 1 à 5), pour des pas de 0,5 à 25 m.
- Six tests de discrimination entre dépressions et buttes (étape 6), avec trois stratégies : permissive, équilibrée, conservatrice.
- Couche des formes rejetées, avec le motif de chaque rejet.
- Interface d'étiquetage à trois classes, dans un panneau ancré non modal (étape 7).
- Entraînement d'une forêt aléatoire de 500 arbres sur la morphométrie et, en option, sur 18 variables de contexte BD TOPO (étape 8).
- Application d'un modèle entraîné, avec classe, score de confiance et probabilité de chaque classe (étape 9).
- Téléchargement des 8 couches BD TOPO par le service WFS de la Géoplateforme IGN, avec cache local.
- Points d'entrée et points bas, avec ajustement manuel des points d'entrée (étape 10).
- Bassins d'alimentation et leur synthèse CSV (étape 11).
- Exports GeoPackage, tableau de contingence par classe de périmètre (discrétisation de Jenks) et journal d'exécution.
- Script `DEADKAT_installation.py` de vérification et d'installation des bibliothèques Python.

## Prévu pour la version 1.1

- Mode de tuilage automatique pour les grandes emprises : tuiles de 5 × 5 km avec 200 m de chevauchement, reprise sur erreur, fusion et dédoublonnage.
- Transmission complète des variables de forme et de contexte BD TOPO à l'étape d'application du modèle.
- Installation des bibliothèques depuis la fenêtre des prérequis.
- Harmonisation des numéros de version affichés et de l'écriture de la résolution dans les noms de fichiers.
- Étude d'une distribution sous forme de plugin QGIS.
