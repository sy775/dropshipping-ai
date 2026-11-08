# Rapport 003-outillage

- Branche : `spec/003-outillage` ; HEAD : `3423040` ; audit du 2026-11-08 21:34 UTC.
- Résultat des étapes déterministes : **OK**.

## Commits

```
3423040 Points mineurs du critic : synthèse pytest dans le rapport, ligne d'état de la spec 003, schéma de niche marqué « à créer »
8067444 Correction (critic, bloquant 2) : une exception du scan ne couvre que la partie exemptée de la ligne (regex ou chaîne exacte), le reste est scanné
0aad7bf Test rouge (critic, bloquant 2) : une clé placée après la regex de redact.py doit être détectée
1bf38e4 Correction (critic, bloquant 1) : le hook suit git switch/checkout au sein d'une commande avant d'évaluer un push
2026d43 Test rouge (critic, bloquant 1) : push vers main après git switch/checkout dans la même commande
7422775 CLAUDE.md : processus spec, plan, validation, implémentation, /audit, rapport, push de spec/*
f1d07a2 CI : pytest, secret_scan (historique complet), mutation_check (complet sur main et la nuit, --changed sur spec/*), notification Telegram
bd61675 /spec, /audit et sous-agent critic
8d662e8 Hooks Claude Code : garde avant outil, pytest en fin de tour, réglages du projet
76b1de7 tools/mutation_check.py : catalogue suivi par le code, id protégés contre origin/main, exécution dans une copie
ce8e5dd tools/secret_scan.py : arbre et historique complet, motifs de redact.py, exceptions explicites
98ee37e Encodage : test du libellé accentué de mutations.yaml et contrôle AST des E/S texte sans encoding="utf-8"
838d1ea Catalogue de mutations des garde-fous (91 entrées)
3e02cbc Specs : docs/specs/NNN (001 déplacée, 002 enregistrée, 003, gabarit) ; fiches de niche dans private/niches/
```

## Étapes de /audit

| Étape | Résultat | Durée |
| --- | --- | --- |
| pytest complet | ok | 49 s |
| secret_scan (arbre et historique) | ok | 2 s |
| mutation_check (complet) | ok | 696 s |

### pytest complet

Commande : `python -m pytest -p no:cacheprovider`

```
...............................................s........................ [ 17%]
........................................................................ [ 34%]
........................................................................ [ 51%]
........................................................................ [ 68%]
........................................................................ [ 85%]
...........................................................              [100%]
418 passed, 1 skipped in 47.83s
```

### secret_scan (arbre et historique)

Commande : `python -m tools.secret_scan`

```
Exceptions explicites :
- journal/redact.py (history, tree) : littéral r"..." des expressions régulières de masquage (re.compile)
- tests/test_alerts.py (history) : faux en-tête Authorization écrit en clair avant la spec 003 (chaîne exacte, historique uniquement)
OK : aucun secret (arbre de travail et historique complet).
```

### mutation_check (complet)

Commande : `python -m tools.mutation_check --base origin/main`

```
**OK** : 91 / 91 mutation(s) tuée(s) (catalogue : 91 entrées ; durée 696 s).
- catalogue absent de origin/main : première introduction (91 entrées), contrôle des id non applicable

Entrées ajoutées : m001, m002, m003, m004, m005, m006, m007, m008, m009, m010, m011, m012, m013, m014, m015, m016, m017, m018, m019, m020, m021, m022, m023, m024, m025, m026, m027, m028, m029, m030, m031, m032, m033, m034, m035, m036, m037, m038, m039, m040, m041, m042, m043, m044, m045, m046, m047, m048, m049, m050, m051, m052, m053, m054, m055, m056, m057, m058, m059, m060, m061, m062, m063, m064, m065, m066, m067, m068, m069, m070, m071, m072, m073, m074, m075, m076, m077, m078, m079, m080, m081, m082, m083, m084, m085, m086, m087, m088, m089, m090, m091
```

## Critic

Sous-agent `critic` (`.claude/agents/critic.md`), lancé sur `git diff origin/main...HEAD` à HEAD `7422775`. Tableau recopié tel quel :

| Écart | Gravité | Preuve | Test qui échouerait |
| --- | --- | --- | --- |
| Le hook laisse passer un push vers main si la commande change d'abord de branche (push sans refspec ou `HEAD` évalué avec la branche courante au moment de l'appel, sans tenir compte d'un `git switch main` / `git checkout main` qui précède dans la même commande). Les règles `permissions.deny` ne couvrent pas `git push` seul. | bloquant | `tools/hooks/pre_tool_use.py:107-114` ; spec §5 : « commandes contenant un push vers main » | `test_push_after_switching_to_main_is_blocked` (`git switch main && git push`, `git checkout main; git push origin HEAD`) : rc == 0 au lieu de 2. |
| L'exception de `journal/redact.py` est plus large que « la regex de redact.py uniquement » : toute ligne qui commence par `re.compile(r"` est exemptée en entier, y compris une vraie clé placée après sur la même ligne, et l'exemption vaut aussi pour l'arbre de travail. | bloquant | `tools/secret_scan.py:38` ; spec §3 | `test_redact_exception_does_not_cover_a_key_after_the_regex` : `scan` renvoie `[]`. |
| `--changed` ne sélectionne que les mutations dont le `file` a changé : une branche qui n'affaiblit que des tests ne relance aucune mutation (CI sur spec/*, /audit par défaut). Conforme au texte de la spec, mais c'est un trou. | à discuter | `tools/mutation_check.py:237` ; `tools/audit.py:121` | Piste : seul `tests/test_calc.py` modifié, `selected == 2` attendu, `0` obtenu. |
| `.env*` et `tests/test_audit_guards.py` ne sont protégés que contre Write/Edit : une écriture par Bash ou PowerShell (`echo x > .env`, `cp a tests/test_audit_guards.py`) passe. | à discuter | `tools/hooks/pre_tool_use.py:159-163` ; spec §5 | Piste : `echo X=1 > .env` doit renvoyer rc == 2. |
| Le critic n'est pas strictement en lecture seule : pytest est accepté avec toute option (`--junitxml=…`, `--basetemp=…`, `-p module`). | à discuter | `tools/hooks/critic_bash.py:46-49` | Piste : `pytest --junitxml=rapport.xml` dans `test_critic_blocked`. |
| Critère « 91 mutations, toutes tuées » : aucun test pytest ne le vérifie ; la preuve est l'exécution consignée dans ce rapport. | à discuter | `tests/test_mutation_check.py:150` | Aucun (686 s : laissé à la CI et à /audit). |
| Une mutation qui atteint le délai est comptée « tuée ». | à discuter | `tools/mutation_check.py:174-175` | Aucun (choix de conception ; listées à part dans le rapport). |
| Rapport pas encore commité, sections manuelles vides. | à discuter | `docs/reports/003-outillage.md` | Aucun (étape du processus). |
| Pas de ligne de synthèse pytest dans le rapport (`-q` de `pytest.ini` plus `-q` de l'audit donnent `-qq`). | mineur | `pytest.ini:4` ; `tools/audit.py:79` | Aucun. |
| CLAUDE.md renvoie à `config/niche_spec.schema.json`, qui n'existe pas. | mineur | diff de `CLAUDE.md` | Aucun. |
| La spec 003 n'a pas de ligne d'état en tête. | mineur | `docs/specs/003-outillage.md:1` | Aucun. |

Synthèse du critic : 2 bloquants, 6 à discuter.

### Tests rouges et corrections (règle « test rouge d'abord »)

**Bloquant 1 : push vers main après changement de branche dans la même commande.**
- Test seul : `2026d43` (`tests/test_hooks.py::test_push_after_switching_to_main_is_blocked`, 3 cas). Échec avant correction :

```
E       assert (0 == 2)
E       assert (0 == 2)
E       assert (0 == 2)
FAILED tests/test_hooks.py::test_push_after_switching_to_main_is_blocked[git switch main && git push]
FAILED tests/test_hooks.py::test_push_after_switching_to_main_is_blocked[git checkout main; git push origin HEAD]
FAILED tests/test_hooks.py::test_push_after_switching_to_main_is_blocked[git switch -q main && git push -u origin]
3 failed, 99 deselected in 0.84s
```

- Correction : `1bf38e4`. Le hook suit `git switch` / `git checkout` (création et `-` compris) segment par segment, et évalue chaque push avec la branche simulée. Après correction : `3 passed, 99 deselected`.

**Bloquant 2 : exception de redact.py trop large.**
- Test seul : `0aad7bf` (`tests/test_secret_scan.py::test_redact_exception_does_not_cover_a_key_after_the_regex`). Échec avant correction :

```
E       AssertionError: assert [] == [('tree', 'jo...l/redact.py')]
E         Right contains one more item: ('tree', 'journal/redact.py')
FAILED tests/test_secret_scan.py::test_redact_exception_does_not_cover_a_key_after_the_regex
1 failed, 8 deselected in 0.26s
```

- Correction : `8067444`. Une exception ne retire plus de la ligne que la partie exemptée : le littéral `r"..."` d'un `re.compile` dans `journal/redact.py`, ou la chaîne exacte du faux en-tête Authorization dans `tests/test_alerts.py` (historique seulement). Le reste de la ligne est scanné. Après correction : les 9 tests du scan passent et le scan du dépôt est propre.

**Points mineurs** corrigés dans `3423040` : synthèse pytest dans le rapport, ligne d'état de la spec 003, schéma de niche marqué « à créer ».

**Points « à discuter » non traités** (décision de Younes) : `--changed` aveugle aux branches qui ne touchent que des tests ; écriture de `.env*` par Bash ou PowerShell ; options de pytest pour le critic ; délai compté comme mutation tuée.

## Hypothèses

- **Catalogue de mutations.** Aucune entrée modifiée (avant / après : néant). Cet audit utilise l'exemption « première introduction », car `tools/mutations.yaml` est absent d'`origin/main`. Après fusion, l'exemption ne s'applique plus.
- **Durée de mutation_check** (hypothèse ouverte de la spec). 330 s pour 91 mutations avec 4 processus, quand pytest durait environ 9 s. 686 s une fois les tests de l'outillage ajoutés : pytest complet passe à 48 s, surtout à cause des dépôts git temporaires et des sous-processus des hooks. En CI (runner à 4 vCPU), le job a un délai de 90 min ; la durée réelle sera à relever au premier passage nocturne.
- **Exceptions du scan de secrets :**
  1. `journal/redact.py`, arbre et historique : le littéral des regex de masquage uniquement.
  2. `tests/test_alerts.py`, historique uniquement : la chaîne exacte du faux en-tête Authorization (le mot `Bearer` suivi de `abc.def-123`), désormais construite à l'exécution dans `tests/fakes.py`.
- **Analyse de texte du hook.** Elle est volontairement prudente : pendant cette branche, elle a bloqué une commande dont un heredoc contenait une option interdite. Les messages de commit qui citent ces options passent donc par `git commit -F`. Elle ne constitue pas une frontière absolue : seconde barrière `permissions.deny`, puis CI.
- **Lancement des hooks.** Les hooks sont lancés par `python` (PATH). Pour la garde PreToolUse, `onFailure: block` les fait échouer en position fermée. Le hook Stop ne bloque pas en cas d'erreur.
- **Référence de comparaison.** `/audit` compare à `origin/main` après `git fetch`, plutôt qu'au `main` local, qui peut être périmé.
- **Exécution de cet audit.** Il a été mené en suivant `.claude/skills/audit/SKILL.md` à la main : le skill a été créé pendant la session, et n'était donc pas chargé au démarrage. L'agent `critic`, lui, est devenu disponible dès sa création. `/spec` et `/audit` seront à vérifier en invocation réelle à la prochaine spec.

## Écarts avec la documentation officielle

- **Tests après édition (spec §5).** Remplacés par un hook `Stop` (décision 5). Doc : https://code.claude.com/docs/en/hooks-guide (« Agent-based hooks », tests vérifiés avant l'arrêt ; « Stop hook hits the block cap », sortie 0 si `stop_hook_active`) et https://code.claude.com/docs/en/hooks (« Exit code 2 behavior per event » : pour Stop, la conversation continue).
- **Bash limité pour le critic (spec §4, « si la doc le permet »).** Le champ `tools` d'un sous-agent ne filtre pas par commande (https://code.claude.com/docs/en/sub-agents). La limite passe donc par un hook PreToolUse dans le frontmatter (https://code.claude.com/docs/en/hooks, « Hooks in skills and agents »). Ces hooks ne s'appliquent qu'après acceptation de la confiance de l'espace de travail.
- **Commandes `/spec` et `/audit`.** Écrites comme skills (`.claude/skills/<nom>/SKILL.md`). Doc : https://code.claude.com/docs/en/skills (« Custom commands have been merged into skills »). Le premier argument est `$0` (indices à partir de 0).
- **Règles `Bash(...)`.** La doc précise qu'elles ne sont « not a security boundary » (https://code.claude.com/docs/en/permissions). D'où la double barrière, hook puis deny. `Edit(...)` couvre Write, Edit, NotebookEdit et MultiEdit, que la doc qualifie de « legacy » ; MultiEdit reste dans le matcher, sans effet s'il n'existe plus.
- **`onFailure: block`.** Exige Claude Code v2.1.295 ou plus (doc hooks) ; la version installée est 2.1.319.
- **Garde `tools/mutations.yaml`.** Elle n'est pas dans le hook (décision 1) : le contrôle vit dans `mutation_check`, sans PyYAML côté système.
