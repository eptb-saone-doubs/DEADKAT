# DEADKAT : Détection Automatisée des Dépressions KArsTiques, version 1.0
# Copyright (C) 2026 EPTB Saône et Doubs
# SPDX-License-Identifier: GPL-3.0-or-later

"""
DEADKAT - Installation et vérification des prérequis
Auteur : Vincent Fister - EPTB Saône et Doubs

Utilisation (une seule fois, avant le premier lancement de DEADKAT) :
  1. Dans QGIS : Extensions > Console Python (Ctrl+Alt+P)
  2. Cliquer sur « Afficher l'éditeur », puis « Ouvrir un script »
  3. Choisir ce fichier, puis cliquer sur « Exécuter le script »

Le script :
  1. dresse l'état des bibliothèques Python requises par DEADKAT ;
  2. installe celles qui manquent dans le Python de QGIS (connexion internet requise) ;
  3. vérifie que le fournisseur GRASS de la boîte à outils de traitement est actif ;
  4. affiche un bilan.

Il ne modifie ni QGIS ni le script DEADKAT. Les versions de NumPy et SciPy
livrées avec QGIS sont conservées, pour ne pas perturber GDAL.
"""

import os
import sys
import site
import tempfile
import importlib
import importlib.util
import subprocess

from qgis.core import Qgis, QgsApplication
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QApplication, QMessageBox

# (nom d'import, nom du paquet pip, usage dans DEADKAT)
BIBLIOTHEQUES = [
    ("numpy", "numpy", "calculs numériques"),
    ("scipy", "scipy", "corrélation de Pearson (TEST6)"),
    ("pandas", "pandas", "tableaux et fichiers d'entraînement"),
    ("jenkspy", "jenkspy", "classification de Jenks"),
    ("sklearn", "scikit-learn", "Random Forest"),
    ("joblib", "joblib", "sauvegarde des modèles"),
]

TITRE = "DEADKAT - Prérequis"


# =============================================================================
# État des bibliothèques
# =============================================================================

def est_installee(nom_import):
    importlib.invalidate_caches()
    return importlib.util.find_spec(nom_import) is not None


def etat():
    return {imp: est_installee(imp) for imp, _, _ in BIBLIOTHEQUES}


def afficher_etat(titre, statut):
    print(f"\n{titre}")
    for imp, paquet, usage in BIBLIOTHEQUES:
        marque = "OK      " if statut[imp] else "MANQUANT"
        print(f"  {marque}  {paquet:<13} {usage}")


# =============================================================================
# Installation
# =============================================================================

def fichier_contraintes():
    """Fige NumPy et SciPy à leur version actuelle (celles de QGIS),
    pour que pip choisisse des versions compatibles des autres bibliothèques."""
    lignes = []
    for nom in ("numpy", "scipy"):
        if est_installee(nom):
            try:
                module = importlib.import_module(nom)
                lignes.append(f"{nom}=={module.__version__}")
            except Exception:
                pass
    if not lignes:
        return None
    chemin = os.path.join(tempfile.gettempdir(), "deadkat_contraintes.txt")
    with open(chemin, "w", encoding="utf-8") as f:
        f.write("\n".join(lignes) + "\n")
    print(f"\nVersions conservées : {', '.join(lignes)}")
    return chemin


def trouver_python():
    """Interpréteur Python de QGIS.
    Sous Windows, sys.executable désigne qgis-bin.exe et non Python."""
    candidats = [
        os.path.join(sys.exec_prefix, "python.exe"),
        os.path.join(sys.exec_prefix, "python3.exe"),
        os.path.join(sys.exec_prefix, "bin", "python3"),
        os.path.join(sys.exec_prefix, "bin", "python"),
        sys.executable,
    ]
    for chemin in candidats:
        if os.path.isfile(chemin) and "qgis" not in os.path.basename(chemin).lower():
            return chemin
    return None


def pip_installer(paquets, contraintes, utilisateur=True):
    """Installe les paquets. Retourne True si pip termine sans erreur."""
    args = ["install"]
    if utilisateur:
        args.append("--user")
    if contraintes:
        args += ["-c", contraintes]
    args += paquets
    print(f"\npip {' '.join(args)}")

    # 1) pip dans le processus QGIS
    try:
        import pip
        code = pip.main(args)
        return code == 0
    except Exception as e:
        print(f"pip interne indisponible ({e}) : essai par l'interpréteur Python de QGIS")

    # 2) pip par l'interpréteur Python de QGIS
    python = trouver_python()
    if python is None:
        print("Interpréteur Python de QGIS introuvable")
        return False
    options = {}
    if os.name == "nt":
        options["creationflags"] = 0x08000000  # pas de fenêtre de console
    resultat = subprocess.run(
        [python, "-m", "pip"] + args,
        capture_output=True, text=True, **options
    )
    print(resultat.stdout[-3000:])
    if resultat.returncode != 0:
        print(resultat.stderr[-3000:])
    return resultat.returncode == 0


def rendre_visible_site_utilisateur():
    """Rend les paquets installés avec --user visibles sans redémarrer,
    le temps de la vérification."""
    try:
        dossier = site.getusersitepackages()
        if os.path.isdir(dossier) and dossier not in sys.path:
            site.addsitedir(dossier)
    except Exception:
        pass
    importlib.invalidate_caches()


# =============================================================================
# GRASS
# =============================================================================

def verifier_grass():
    """True si r.watershed est disponible dans la boîte à outils de traitement."""
    try:
        registre = QgsApplication.processingRegistry()
        for algo in ("grass7:r.watershed", "grass:r.watershed"):
            if registre.algorithmById(algo) is not None:
                return True
        return False
    except Exception:
        return None


# =============================================================================
# Programme principal
# =============================================================================

def main():
    print("=" * 70)
    print(TITRE)
    print("=" * 70)
    print(f"QGIS   : {Qgis.QGIS_VERSION}")
    print(f"Python : {sys.version.split()[0]} ({sys.exec_prefix})")

    avant = etat()
    afficher_etat("1. État initial", avant)

    manquantes = [paquet for imp, paquet, _ in BIBLIOTHEQUES if not avant[imp]]
    installation_tentee = False
    installation_ok = True

    if manquantes:
        reponse = QMessageBox.question(
            None, TITRE,
            "Bibliothèques manquantes :\n  " + "\n  ".join(manquantes) + "\n\n"
            "Les installer maintenant ?\n\n"
            "Connexion internet requise. QGIS peut sembler figé une à "
            "quelques minutes pendant le téléchargement.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
        )
        if reponse == QMessageBox.Yes:
            installation_tentee = True
            print("\n2. Installation")
            contraintes = fichier_contraintes()
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                installation_ok = pip_installer(manquantes, contraintes, utilisateur=True)
                if not installation_ok:
                    print("Échec avec --user : nouvel essai sans --user")
                    installation_ok = pip_installer(manquantes, contraintes, utilisateur=False)
            finally:
                QApplication.restoreOverrideCursor()
            rendre_visible_site_utilisateur()
    else:
        print("\n2. Installation : rien à installer")

    apres = etat()
    afficher_etat("3. État final", apres)

    grass = verifier_grass()
    if grass is True:
        texte_grass = "OK"
    elif grass is False:
        texte_grass = ("MANQUANT : Extensions > Installer/Gérer les extensions > "
                       "Installées > cocher « GRASS GIS Processing Provider »")
    else:
        texte_grass = "non vérifiable"
    print(f"\n4. Fournisseur GRASS : {texte_grass}")

    # Bilan
    lignes = [f"QGIS {Qgis.QGIS_VERSION}, Python {sys.version.split()[0]}", ""]
    for imp, paquet, _ in BIBLIOTHEQUES:
        lignes.append(f"{'OK' if apres[imp] else 'MANQUANT'}  :  {paquet}")
    lignes.append(f"\nGRASS : {texte_grass}")

    tout_ok = all(apres.values()) and grass is not False
    if installation_tentee and installation_ok:
        lignes.append("\nRedémarrez QGIS avant de lancer DEADKAT.")
    if installation_tentee and not installation_ok:
        lignes.append("\nL'installation a échoué : voir le détail dans la console Python.")
    if tout_ok and not installation_tentee:
        lignes.append("\nTous les prérequis sont en place.")

    print("\n" + "\n".join(lignes))
    print("=" * 70)

    if tout_ok:
        QMessageBox.information(None, TITRE, "\n".join(lignes))
    else:
        QMessageBox.warning(None, TITRE, "\n".join(lignes))


main()
