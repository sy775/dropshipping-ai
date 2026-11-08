# Spec 003 : outillage du développement (specs, CI, hooks, critic, /spec, /audit)

## Objectif
Réduire le temps de Younes par cycle à environ 5 minutes sans supprimer aucune validation humaine
(plan et fusion dans main). Toutes les vérifications déterministes deviennent automatiques.

## Avant de coder
Lis la documentation officielle de Claude Code (hooks, sous-agents, commandes slash ou skills, fichier de
réglages du projet) et cite les pages. Propose un plan et attends ma validation (règle de CLAUDE.md).

## 1. Specs et rapports
- docs/specs/NNN-titre.md. Déplace docs/spec-llm-adapter.md vers docs/specs/001-llm-adapter.md et mets à jour
  toutes les références. Gabarit docs/specs/_template.md : Objectif, Périmètre et hors périmètre,
  Entrées-sorties, Règles, Critères de test, Hypothèses ouvertes.
- docs/reports/NNN-titre.md : rapport de fin, commité avec le code (commits, pytest, résultat de /audit,
  hypothèses, écarts avec la doc officielle).

## 2. Commande /spec <NNN-titre>
Enregistre le texte collé dans docs/specs/, crée la branche spec/NNN-titre depuis main à jour, lit CLAUDE.md et
la spec, propose un plan, puis S'ARRÊTE jusqu'à validation.

## 3. Commande /audit
Dans l'ordre, chaque étape bloquante :
- pytest complet ;
- tools/secret_scan.py : arbre et historique complet, mêmes motifs que journal/redact.py, avec une liste
  d'exceptions explicite (la regex de journal/redact.py uniquement) ;
- tools/mutation_check.py, lisant tools/mutations.yaml (déjà présent, 91 mutations : id, description, file, old,
  new). Une mutation dont `old` n'est pas présent exactement une fois est un ÉCHEC (le catalogue doit suivre le
  code) ; une mutation qui ne fait échouer aucun test est un ÉCHEC. Option --only m001,m002 et --changed (mutations
  des fichiers modifiés depuis main) ;
- sous-agent critic sur `git diff main...HEAD`.
Le résultat complet va dans le rapport.

## 4. Sous-agent critic (.claude/agents/critic.md)
Lecture seule : aucun outil d'écriture ou d'édition ; Bash limité à git diff, git log et pytest si la doc le permet.
Entrées : diff, spec, CLAUDE.md. Sortie : tableau Écart | Gravité | Preuve | Test qui échouerait.
Vérifie aussi : assertions affaiblies dans des tests modifiés, critères de test de la spec non couverts,
règles de CLAUDE.md, secrets. Règle : un point n'est bloquant que s'il est accompagné d'un test qui échoue.

## 5. Hooks (réglages du projet, scripts dans tools/hooks/)
- Avant outil, bloquant : écriture ou édition de tests/test_audit_guards.py, de tools/mutations.yaml (sauf ajout
  d'entrées, que tu dois signaler) et de tout fichier .env* hors .env.example ; commandes contenant un push vers
  main, `--force`, `gh repo edit`, `gh secret`, `git filter-repo`, `gh pr merge`.
- Après édition d'un .py : pytest -x -q, résultat renvoyé à Claude Code.
- Tests unitaires de chaque script de hook (entrée JSON simulée).

## 6. CI (.github/workflows/ci.yml)
- Déclencheurs : push sur toutes les branches, schedule quotidien, workflow_dispatch. Jamais pull_request_target.
- permissions: contents: read. Python 3.12, cache pip.
- Jobs : pytest ; secret_scan (fetch-depth: 0) ; mutation_check complet sur main et chaque nuit, `--changed` sur
  les branches spec/*.
- Notification Telegram du statut (secrets TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID ; étape ignorée s'ils sont
  absents ; aucun secret affiché dans les logs).

## 7. CLAUDE.md
Ajoute le processus : spec, plan, validation, implémentation, /audit, rapport, push de la branche spec/*.
Jamais de commit direct sur main ni de fusion. Règle : si une instruction est contradictoire, ambiguë ou fausse,
le dire AVANT d'exécuter.

## 8. Encodage
Lis et écris tous les fichiers texte (YAML, Markdown, rapports) avec encoding="utf-8" explicite, y compris sous
Windows ; ajoute un test qui charge tools/mutations.yaml et vérifie un libellé accentué.

## Critères de test
- mutation_check sur le code actuel : 91 mutations, toutes tuées ; une entrée au `old` introuvable fait échouer.
- secret_scan : une fausse clé construite dans un dépôt temporaire est détectée ; la regex de redact.py est ignorée.
- Hooks : chaque blocage testé ; une commande autorisée (git push origin spec/xxx) passe.
- Le workflow CI est valide (YAML chargé, déclencheurs et permissions vérifiés par un test).
- Rapport docs/reports/003-outillage.md produit par /audit.
- Le chargement de tools/mutations.yaml restitue correctement un libellé accentué.

## Hypothèses ouvertes
Syntaxe exacte des hooks et des sous-agents : selon la doc officielle. Durée de mutation_check en CI à mesurer.

---

## Décisions de validation (2026-11-08), prioritaires sur le texte ci-dessus

1. Catalogue de mutations : `old`, `new` et `file` d'une entrée existante sont modifiables ; jamais supprimer une
   entrée ni changer son `id`. Contrôle dans mutation_check (pas dans le hook) : tous les `id` de
   `git show origin/main:tools/mutations.yaml` existent encore, aucune entrée n'a `new == old`, aucun `id` en double.
   Chaque entrée modifiée est listée dans le rapport (avant / après). Le hook ne bloque plus tools/mutations.yaml.
   Exemption « première introduction » uniquement si le fichier est absent d'origin/main ; dès qu'il y existe, aucune
   exemption. Si le fichier est absent de la branche, mutation_check échoue.
2. Secret scan : exception pour la chaîne exacte du faux jeton d'en-tête Authorization de tests/test_alerts.py, dans ce fichier exact,
   valable uniquement pour les blobs de l'historique, jamais pour l'arbre de travail. Dans l'arbre, ce faux jeton est
   construit à l'exécution (concaténation). Exception listée dans le rapport.
3. Secret scan : clés et jetons uniquement (pas les emails).
4. Critic : hooks dans le frontmatter pour limiter Bash. Règle « test rouge d'abord » : pour tout point bloquant,
   commit du test seul (échec montré), puis correction dans un commit séparé ; les deux dans le rapport.
5. Tests automatiques : hook Stop (fin de tour) plutôt que PostToolUse, avec garde `stop_hook_active`.
6. Spec 002 enregistrée en docs/specs/002-evaluation-criteres.md, état d'implémentation en tête.
7. Pas de dossier specs/ à la racine : fiches de niche dans private/niches/ (ignoré), schéma JSON des niches dans
   config/niche_spec.schema.json.
8. Fin de branche : /audit, rapport docs/reports/003-outillage.md, push de spec/003-outillage. Pas de fusion.
