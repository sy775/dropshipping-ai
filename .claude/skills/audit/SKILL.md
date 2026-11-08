---
name: audit
description: Audit de fin de branche spec/* - pytest, scan de secrets, contrôle de mutations, puis sous-agent critic ; écrit docs/reports/NNN-titre.md.
argument-hint: "[--full]"
disable-model-invocation: true
---

Audit de la branche courante. Arguments : `$ARGUMENTS` (`--full` : toutes les mutations ; par défaut `--changed`,
sauf si le catalogue a changé depuis origin/main, auquel cas toutes).

1. Vérifie que la branche courante est `spec/NNN-titre` et que l'arbre de travail est propre. Sinon, arrête-toi.
   `git fetch origin` pour que `origin/main` soit à jour.
2. Lance `.venv/Scripts/python.exe -m tools.audit $ARGUMENTS` (sous Linux : `.venv/bin/python`). Le script
   enchaîne pytest complet, `tools/secret_scan.py` puis `tools/mutation_check.py` ; chaque étape est bloquante
   (arrêt à la première en échec). Il écrit `docs/reports/NNN-titre.md` avec la sortie complète de chaque étape.
3. Si une étape a échoué : ne lance pas le critic. Donne l'échec à Younes, propose la correction, arrête-toi.
4. Sinon, lance le sous-agent `critic` (outil Agent, `subagent_type: critic`) avec ce prompt :
   « Relis la branche courante contre origin/main selon tes instructions. Spec : docs/specs/NNN-titre.md. »
5. Recopie son tableau tel quel dans la section « Critic » du rapport.
6. Pour chaque point **bloquant** : règle « test rouge d'abord ».
   - Écris le test décrit, commite-le seul (`Test rouge : ...`), lance-le et recopie l'échec dans le rapport.
   - Corrige dans un commit séparé (`Correction : ...`), relance le test (vert) et recopie le résultat.
   - Relance ensuite tout l'audit (étape 2) : le rapport final doit refléter l'état final.
7. Complète les sections « Hypothèses » et « Écarts avec la documentation officielle » du rapport (pages citées).
8. Commite le rapport : `Rapport NNN-titre`. Pousse la branche : `git push -u origin spec/NNN-titre`.
   Jamais de push vers main, jamais de fusion : la fusion reste à Younes.
9. Termine par 3 lignes : ce qui a été fait, résultat de l'audit, comment le vérifier.
