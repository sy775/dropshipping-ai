---
name: spec
description: Enregistre une spec collée dans docs/specs/NNN-titre.md, crée la branche spec/NNN-titre depuis origin/main à jour, propose un plan puis s'arrête jusqu'à validation.
argument-hint: <NNN-titre> <texte de la spec>
disable-model-invocation: true
---

Spec demandée : `$0` (format attendu `NNN-titre`, par exemple `004-sourcing-agent`).
Texte complet reçu (le nom puis la spec collée) :

$ARGUMENTS

Procédure, dans l'ordre. Arrête-toi et explique si une étape échoue.

1. Vérifie que `$0` respecte `^\d{3}-[a-z0-9-]+$` et que `docs/specs/$0.md` n'existe pas déjà. Sinon, arrête-toi.
2. Vérifie que l'arbre de travail est propre (`git status --porcelain` vide). Sinon, arrête-toi et liste les
   fichiers : ne jamais stasher ni écraser sans accord.
3. `git fetch origin`, puis `git switch -c spec/$0 origin/main`, puis `git branch --unset-upstream`
   (la branche sera poussée vers `origin spec/$0`, jamais vers main).
4. Enregistre le texte de la spec (tout ce qui suit `$0` ci-dessus, sans le modifier) dans `docs/specs/$0.md`,
   en UTF-8, avec en tête la ligne `> État : en cours`. Si le texte ne suit pas le gabarit
   `docs/specs/_template.md`, garde-le tel quel et signale les rubriques manquantes.
5. Commit : `Spec $0 : enregistrement` (le fichier seul).
6. Lis `CLAUDE.md` et `docs/specs/$0.md`. Si une instruction est contradictoire, ambiguë ou fausse, liste-la
   en premier, avec ta recommandation.
7. Propose un plan : petits commits testables, tests prévus pour chaque critère de la spec, fichiers touchés,
   pages de documentation consultées si la spec en dépend.
8. **ARRÊTE-TOI.** N'écris aucun code avant la validation explicite de Younes.
