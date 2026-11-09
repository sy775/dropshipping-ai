# Rapport 003-outillage

- Branche : `spec/003-outillage` ; HEAD : `696a90c` ; audit du 2026-11-09 14:20 UTC.
- Résultat des étapes déterministes : **OK**.
- Après cet audit : la seconde passe du critic a trouvé 4 bloquants, voir « Critic, seconde passe ». Les tests rouges
  sont commités (`5b4db39`), et les correctifs attendent dans `docs/reports/003-correctifs/`. Ils touchent des fichiers
  verrouillés, donc Younes les applique lui-même. Tant que ce n'est pas fait, pytest et la CI de la branche sont
  rouges. `/audit` n'a pas été relancé.

## Commits

```
696a90c Verrou des garde-fous de l'outillage (hook, réglages Claude Code, critic, CI, audit) : écriture bloquée par outil et par mention shell, lecture git permise, règles deny, filet de l'audit et de la CI, mutations m104-m109
eac808b Mutations m092-m103 : règle par mention, lecture des fichiers d'environnement, filet de l'audit et de la CI
718e23d Filet hors agent : l'audit et la CI (toute branche sauf main et human/*) échouent si un fichier protégé présent sur origin/main est modifié ou supprimé
2a9a4ea Correction (relecture externe) : le hook bloque toute commande shell qui mentionne un fichier protégé (lecture git seule permise pour les garde-fous d'audit, rien pour les fichiers d'environnement) et la lecture de ces derniers par Read, Grep ou Glob
d5d486a Test rouge (relecture externe) : une commande Bash ou PowerShell qui mentionne un fichier protégé, et une lecture de .env* par Read, Grep ou Glob, doivent être bloquées
4b8c55d Rapport 003-outillage
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
| fichiers protégés inchangés | ok | 0 s |
| pytest complet | ok | 117 s |
| secret_scan (arbre et historique) | ok | 2 s |
| mutation_check (complet) | ok | 2110 s |

### fichiers protégés inchangés

Commande : `python -m tools.audit --protected-only --base origin/main`

```
aucun fichier protégé modifié
```

### pytest complet

Commande : `python -m pytest -p no:cacheprovider`

```
...............................................s........................ [ 11%]
........................................................................ [ 23%]
........................................................................ [ 35%]
........................................................................ [ 47%]
........................................................................ [ 58%]
........................................................................ [ 70%]
........................................................................ [ 82%]
........................................................................ [ 94%]
...................................                                      [100%]
610 passed, 1 skipped in 116.44s (0:01:56)
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
**OK** : 109 / 109 mutation(s) tuée(s) (catalogue : 109 entrées ; durée 2109 s).
- catalogue absent de origin/main : première introduction (109 entrées), contrôle des id non applicable

Entrées ajoutées : m001, m002, m003, m004, m005, m006, m007, m008, m009, m010, m011, m012, m013, m014, m015, m016, m017, m018, m019, m020, m021, m022, m023, m024, m025, m026, m027, m028, m029, m030, m031, m032, m033, m034, m035, m036, m037, m038, m039, m040, m041, m042, m043, m044, m045, m046, m047, m048, m049, m050, m051, m052, m053, m054, m055, m056, m057, m058, m059, m060, m061, m062, m063, m064, m065, m066, m067, m068, m069, m070, m071, m072, m073, m074, m075, m076, m077, m078, m079, m080, m081, m082, m083, m084, m085, m086, m087, m088, m089, m090, m091, m092, m093, m094, m095, m096, m097, m098, m099, m100, m101, m102, m103, m104, m105, m106, m107, m108, m109
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

### Défaut trouvé par la relecture externe, signalé par le critic puis écarté par l'auteur

Le hook ne protégeait `tests/test_audit_guards.py` et `.env*` que contre Write et Edit. La relecture externe a fait
passer cinq commandes avec le code de sortie 0 :

- `printf '\n' >> tests/test_audit_guards.py`
- `echo KEY=x > .env`
- `cp /tmp/x tests/test_audit_guards.py`
- `sed -i 's/a/b/' tests/test_audit_guards.py`
- `python -c "open('.env','w').write('x')"`

**Le critic n'a pas manqué ce défaut.** Il l'a signalé dans le tableau ci-dessus (ligne « `.env*` et
`tests/test_audit_guards.py` ne sont protégés que contre Write/Edit »), mais en « à discuter » et non en bloquant.
L'auteur (Claude) l'a ensuite écarté parmi les « points à discuter non traités », puis a affirmé à tort que le hook
« bloque toute écriture, y compris via Bash ».

Corrections :
- **Test rouge commité seul** (`d5d486a`) : 39 échecs et 130 réussites sur `tests/test_hooks.py`. Il couvre les cinq
  commandes ci-dessus, `Add-Content` et `Set-Content` en PowerShell, plusieurs formes de chemin, une lecture git
  détournée en écriture, et la lecture de `.env*` par le shell, Read, Grep et Glob.
- **Correction** (`2a9a4ea`) : toute commande Bash ou PowerShell qui mentionne un fichier protégé est bloquée. La
  mention compte en position fermée, quelle que soit la forme du chemin.
  - `.env*` (sauf `.env.example`) est bloqué même en lecture.
  - Pour `tests/test_audit_guards.py`, seuls `git diff|log|show|status` restent permis, sans redirection ni
    substitution.
  - Read, Grep et Glob sont bloqués sur `.env*`. Raison : d'après la doc, les règles deny `Read` ne s'appliquent à
    Grep et Glob qu'en « best-effort » (https://code.claude.com/docs/en/permissions, « What Read and Edit rules
    cover »). Les règles `Read(.env*)` et `Read(**/.env*)` sont ajoutées en plus.
- **Filet hors agent** (`718e23d`) : l'étape « fichiers protégés inchangés » de `/audit` et le job CI
  `protected_files` échouent si un fichier protégé présent sur origin/main est modifié ou supprimé. Le contrôle
  s'applique à toutes les branches sauf main et `human/*`.
- **Mutations** m092 à m103 (`eac808b`) : 12 sur 12 tuées.
- **Faille trouvée par l'auteur pendant la correction** : l'agent pouvait modifier lui-même le hook, les réglages, le
  critic, la CI ou l'audit, et ainsi désactiver ses garde-fous. Ces fichiers sont verrouillés depuis `696a90c`, avec
  les mutations m104 à m109 (6 sur 6 tuées).

### Critic, seconde passe (HEAD `696a90c`)

Tableau du critic, recopié tel quel, sauf la colonne « Test qui échouerait » des bloquants et du point sur les usages
légitimes : le code des tests proposés y est résumé par son nom et ses cas. Les tests effectivement écrits sont dans
`5b4db39`.

| Écart | Gravité | Preuve | Test qui échouerait |
| --- | --- | --- | --- |
| Le hook peut être neutralisé sans toucher à un fichier verrouillé. Claude Code lance `python "<projet>/tools/hooks/pre_tool_use.py"` sans `-I`, donc Python cherche d'abord les modules dans `tools/hooks/`. Si on y place un module `shlex.py` (ou `json.py`, `re.py`), il est importé à la place de la bibliothèque standard ; il peut faire `sys.exit(0)` et tout est alors autorisé. Ce chemin n'est pas protégé, et le contrôle de l'audit et de la CI ne voit pas un fichier nouveau. | bloquant | `.claude/settings.json:33` `"command": "python \"$CLAUDE_PROJECT_DIR/tools/hooks/pre_tool_use.py\""` ; `pre_tool_use.py:23` `import shlex` ; `check_path` ne contrôle que PROTECTED et `.env*`. | `tests/test_hooks.py::test_hook_ignores_module_planted_next_to_it` : copie du hook dans un dossier temporaire, `shlex.py` déposé à côté, commande lancée telle qu'écrite dans `settings.json`. Échec attendu : le `shlex.py` déposé est importé et le hook sort avec 0. Correction possible : `python -I` dans les commandes de hook de `settings.json` et du frontmatter de `critic.md`. Ces deux fichiers sont verrouillés, donc c'est un diff à appliquer par Younes. |
| Rien n'empêche d'écrire `.claude/settings.local.json`. Ce fichier n'est ni verrouillé ni contrôlé par le filet. Avec `{"disableAllHooks": true}`, il désactive tous les hooks (réglage Claude Code). Il n'est pas dans GUARDED, donc ni l'audit ni la CI ne le voient. | bloquant | `pre_tool_use.py:35-43` (PROTECTED sans `settings.local.json`) ; `check_path:72-78`. | `test_local_settings_write_is_blocked` (chemins relatif et absolu, outil Write) : `check_path` renvoie None, donc rc vaut 0. |
| Le hook n'empêche pas de pousser une branche `human/*`, alors que c'est justement la branche exemptée par le job CI `protected_files`. Il suffit donc de nommer sa branche `human/x` pour échapper au filet. C'est aussi contraire à CLAUDE.md (« Push de la branche spec/NNN-titre uniquement »). | bloquant | `ci.yml` `if: github.ref != 'refs/heads/main' && !startsWith(github.ref, 'refs/heads/human/')` ; `_check_push:191-220` ne bloque que main. | `test_push_to_human_branch_is_blocked` (`git push origin HEAD:human/x`, `git push -u origin human/x`) : rc vaut 0. Le test existant `git push origin spec/xxx` doit continuer à passer. |
| La règle par mention ne voit que le nom du fichier. Restaurer ou copier le dossier parent modifie donc un fichier verrouillé sans le nommer. Le cas fichier, `git checkout origin/main -- {}`, est testé et bloqué ; le cas dossier passe. | bloquant | `pre_tool_use.py:97` `_basename_mention(p.rsplit("/", 1)[-1])`. | `test_restoring_parent_dir_of_locked_file_is_blocked` (`git checkout origin/main -- tools/hooks`, `git restore --source=origin/main -- .claude`, `git checkout HEAD~1 -- .github`, `cp -r /tmp/h/. tools/hooks/`) : rc vaut 0. Les mêmes commandes avec `git diff`, `git log`, `git show` ou `git status` doivent rester permises. |
| Le blocage `.env*` ne voit pas les noms construits par des guillemets ou un échappement. `cat ."env"`, `cat .\env` (le `\` devient `/`, soit `./env`) et `cat .e*` lisent tous `.env` en bash. Même problème en écriture : `cp x tools/audit.p""y`. Le commentaire du hook cite globs et chemins assemblés comme limite connue, pas le simple échappement. | à discuter | `pre_tool_use.py:88` `_ENV_MENTION` ; `:122` `command.replace("\\", "/")`. | Test possible : `cat .""env`, `cat .\env` et `cat .e*` ajoutés à `test_shell_mention_of_protected_file_is_blocked`. Ils échouent aujourd'hui. Le texte ne pourra jamais être une barrière complète : à trancher ce qu'on accepte comme limite écrite dans le rapport. |
| Le filet CI et audit s'appuie sur la copie de la branche elle-même. Le job exécute le `tools/audit.py` de la branche, avec la liste GUARDED de la branche, depuis le `ci.yml` de la branche. Une branche qui modifie ces fichiers peut donc désactiver le contrôle qui devait la signaler. | à discuter | `ci.yml` `run: python -m tools.audit --protected-only --base origin/main` ; `audit.py` `GUARDED = (...)`. | Pas de test simple. Pistes : faire lire GUARDED et le script de contrôle depuis `git show origin/main:...`, ou rendre le statut obligatoire côté GitHub (ruleset). |
| `tools/secret_scan.py`, `tools/mutation_check.py`, `tools/hooks/stop_pytest.py` et `.claude/commands/audit.md` ne sont pas verrouillés. On peut donc affaiblir ce que l'audit exécute, par exemple un `mutation_check` qui déclare tout tué. Seul le diff relu par le critic le montrerait. | à discuter | `pre_tool_use.py:35-43` ; `test_stop_hook_is_not_locked` (exemption voulue pour la spec 004). | Pas de test : c'est un choix de périmètre. |
| Usages légitimes bloqués : (a) lancer seul le fichier des garde-fous, `python -m pytest -q tests/test_audit_guards.py`, y compris pour le critic qui a droit à pytest ; (b) une lecture git suivie d'une redirection d'erreur, `git log -- tools/audit.py 2>&1` (refusée à cause du `>`) ; (c) `git commit -m "... .env* ..."` ou un message qui cite un fichier verrouillé, comme les messages de d5d486a et 2a9a4ea (contournement : `-F`) ; (d) tout `settings.json`, `ci.yml` ou `audit.py` ailleurs dans le dépôt (par exemple `cat config/settings.json`), car seul le nom de fichier est comparé. | à discuter | `pre_tool_use.py:126-130` (seules les lectures git sont exemptées) ; `:90` `[<>`]` ; `:97`. | `test_legit_mentions_pass` (`python -m pytest -q tests/test_audit_guards.py`, `cat config/settings.json`) : rc vaut 2. |
| Grep sans `glob` sur la racine (`Grep(pattern="API_KEY", path=".")`) n'est pas bloqué. Si l'outil Grep inclut les fichiers cachés ou ignorés par git, il renvoie des lignes de `.env`. Je n'ai pas pu vérifier ce comportement. | à discuter | `pre_tool_use.py:135` `"Grep": ("path", "glob")`. | Pas de test fiable sans connaître les options rg de l'outil Grep. |
| Écarts de documentation et de processus. Les décisions de la relecture externe (sept fichiers verrouillés, blocage de Read, Grep et Glob sur `.env*`, job `protected_files`) ne figurent pas dans les « Décisions de validation » de la spec. CLAUDE.md, ligne 44, dit encore que seuls `tests/test_audit_guards.py` et `.env*` sont protégés, juste au-dessus de la nouvelle ligne 45. Enfin, `pre_tool_use.py`, `settings.json` et `audit.py` étant désormais verrouillés pour Claude, la règle « test rouge commité seul, puis correction » ne peut être suivie qu'en partie pour les points bloquants ci-dessus : la correction devra être un diff écrit dans le rapport et appliqué par Younes. | mineur | `CLAUDE.md:44-48` ; `docs/specs/003-outillage.md:79-97`. | Aucun (documentation). |

Synthèse du critic : 4 bloquants, 5 à discuter.

Note : les tests proposés par le critic ont été écrits sous les noms suivants, élargis selon les consignes de Younes.
- Bloquant 1 : `test_hook_ignores_module_planted_next_to_it` et `test_every_hook_command_runs_python_isolated`.
- Bloquant 2 : `.claude/settings.local.json` ajouté à `LOCKED`, donc couvert par les tests paramétrés
  `test_locked_files_*`.
- Bloquant 3 : `test_only_push_of_one_spec_branch_to_origin_is_allowed`.
- Bloquant 4 : `test_locked_dirs_are_blocked_for_shell` et `test_locked_dirs_git_read_and_subpaths_pass`.

### Bloquants de la seconde passe : tests rouges et correctifs à appliquer par Younes

Les quatre corrections touchent des fichiers verrouillés : `tools/hooks/pre_tool_use.py`, `.claude/settings.json`,
`.claude/agents/critic.md` et `tools/audit.py`. Claude ne peut pas les appliquer. La règle « test rouge d'abord » est
donc suivie ainsi :

**1. Test rouge commité seul** (`5b4db39`, `tests/test_hooks.py`) : `64 failed, 278 passed`. Les échecs se
répartissent ainsi :

```
  1 test_every_hook_command_runs_python_isolated            (bloquant 1 : python -I)
  1 test_hook_ignores_module_planted_next_to_it             (bloquant 1 : shlex.py déposé importé)
 10 test_locked_dirs_are_blocked_for_shell                  (bloquant 4)
 24 test_locked_files_are_blocked_for_shell                 (bloquant 2 et nouveaux fichiers verrouillés)
 12 test_locked_files_are_blocked_for_write_tools           (idem)
  1 test_locked_list_is_one_file_shared_by_hook_and_audit   (liste unique)
 15 test_only_push_of_one_spec_branch_to_origin_is_allowed  (bloquant 3 et règles de push)
```

Un paramètre existant a été retiré de `test_allowed_commands_pass` : `git push` sans argument. La consigne exige
maintenant la forme explicite `git push origin spec/<nom>` ; ce cas fait donc partie des cas à bloquer.

**2. Correctifs** dans `docs/reports/003-correctifs/`, au format `git diff` (chemins `a/` et `b/` depuis la racine).
Ils sont écrits pour s'appliquer dans l'ordre sur `5b4db39` :

| Patch | Fichiers | Contenu |
| --- | --- | --- |
| `01-python-isole-et-deny.patch` | `.claude/settings.json`, `.claude/agents/critic.md` | `python -I` pour les trois hooks (bloquant 1) ; règles deny `Edit` pour les fichiers ajoutés à la liste |
| `02-liste-verrouillee.patch` | `tools/locked_paths.txt` (nouveau), `CLAUDE.md` | Liste unique des fichiers et dossiers verrouillés, verrouillée elle-même. Ajouts : `.claude/settings.local.json` (bloquant 2), `tools/secret_scan.py`, `tools/mutation_check.py`, dossiers parents (bloquant 4) |
| `03-hook.patch` | `tools/hooks/pre_tool_use.py`, `tests/test_hooks.py` | Le hook lit la liste (liste illisible : tout est bloqué), bloque la mention d'un dossier verrouillé et n'accepte que `git push [-u] origin spec/<nom>` (bloquant 3) ; test « liste illisible » |
| `04-filet.patch` | `tools/audit.py`, `tests/test_audit_tool.py` | Le filet contrôle tous les fichiers de la liste : union de la liste de la branche et de celle de la base, liste introuvable = échec. Catalogue en ajout seul |
| `05-mutations.patch` | `tools/mutations.yaml` | m104 à m109 suivent la liste ; m110 à m118 couvrent `-I`, liste, dossiers, push et catalogue |

- **Validation.** Aucun patch n'a été appliqué, ni dans le dépôt ni dans une copie : la consigne interdit tout
  worktree, toute copie et tout `git apply` touchant des fichiers verrouillés. Seule la cohérence interne a été
  vérifiée : nombre de lignes de chaque hunk (21 hunks) et fins de ligne LF. Les lignes de contexte ont été recopiées
  des fichiers lus, et les numéros de ligne calculés à la main. Les diffs des fichiers non verrouillés
  (`tests/test_hooks.py`, `tests/test_audit_tool.py`, `CLAUDE.md`, `tools/mutations.yaml`) viennent de `git diff`.
  La relecture externe les validera en clonant le dépôt.
- **Après application** : relancer `python -m tools.audit --full`, puis le critic. Les tests de `5b4db39` et des
  patches doivent passer, et les mutations m092 à m118 être tuées.
- **`-I` et imports locaux.** Les hooks n'importent que la bibliothèque standard (test existant
  `test_hooks_use_standard_library_only`) ; le hook trouve sa liste par un chemin calculé depuis son propre fichier,
  pas par un import. `-I` ne casse donc rien, et le dossier du script n'est pas remis dans `sys.path`.

Choix d'interprétation, à confirmer par Younes avant d'appliquer :
- **Dossiers verrouillés** (`tools/`, `tools/hooks/`, `tests/`, `.claude/`, `.claude/agents/`, `.github/`,
  `.github/workflows/`) :
  - Le hook bloque leur mention seule dans une commande shell : `git checkout X -- tools/hooks`, `cp -r … .claude/`.
    Il ne bloque pas les fichiers qu'ils contiennent, sauf ceux de la liste.
  - Le filet ne contrôle que les fichiers. S'il contrôlait les dossiers entiers, il bloquerait
    `tools/hooks/stop_pytest.py` (à laisser modifiable pour la spec 004), les skills et tous les tests.
- **Catalogue en ajout seul.** C'est contraire à l'ancienne règle de CLAUDE.md (« `old`, `new`, `file`
  modifiables »). Le patch 02 réécrit cette règle : après fusion, modifier une entrée existante reviendra à Younes.
  Conséquence : si une spec renomme du code visé par une mutation, son `old` ne sera plus trouvé, et seul Younes
  pourra corriger l'entrée.
- **Push.** `-u` (ou `--set-upstream`) reste permis : le skill `/audit` pousse avec `git push -u origin spec/NNN-titre`.
- **Faux positifs supplémentaires.** Avec `tests/` et `tools/` verrouillés comme dossiers, `pytest tests` et `ls tools`
  sont bloqués. `pytest`, `pytest -q tests/test_x.py` et `python -m tools.audit` restent permis.
- **`.env.example` illisible par l'outil Read.** Une règle deny l'emporte sur toute autre règle : `Read(.env*)` interdit
  donc aussi `.env.example` à l'outil Read. Le hook, lui, le laisse passer, et `cat .env.example` reste permis.

### Limites connues (écrites, non traitées)

Ces contrôles réduisent le risque mais ne forment pas une frontière de sécurité.

- **Détection par le texte incomplète.**
  - Échappements et guillemets : `cat .""env`, `cat .\env`.
  - Globs et variables : `cat .e*`, `f=.e; cat ${f}nv`.
  - `grep -r` sur la racine, et l'outil Grep sans motif de fichiers sur `.`. Ils dépendent des fichiers ignorés par
    git, comportement non vérifié pour l'outil Grep.
  - Copie de dossiers entiers depuis un autre dossier courant (`cd tools && cp -r x hooks`).
  - Commandes qui réécrivent tout l'arbre sans nommer de chemin (`git checkout X -- .`, `git reset --hard X`,
    `git stash pop`).
- **Le filet s'exécute avec le code de la branche.** Le job CI lance le `tools/audit.py` et le `ci.yml` de la branche.
  Une branche qui les modifie peut désactiver le contrôle censé la signaler. Seul un statut obligatoire côté GitHub
  (ruleset sur main) rendrait le filet vraiment indépendant : à configurer par Younes. Par ailleurs, les fichiers
  verrouillés sont introduits par cette branche ; le filet ne les couvre donc qu'une fois la branche fusionnée.
- **Faux positifs acceptés.**
  - `python -m pytest tests/test_audit_guards.py` est bloqué ; la suite complète reste permise.
  - Une lecture git suivie d'une redirection (`git log -- tools/audit.py 2>&1`) est bloquée.
  - Un message de commit qui cite `.env` ou un fichier verrouillé est bloqué : utiliser `git commit -F`.
  - Un autre fichier portant le même nom qu'un fichier verrouillé (`config/settings.json`, par exemple) est bloqué.
  - Les chemins de configuration de l'utilisateur (`~/.claude/...`) sont bloqués.
- **Points « à discuter » de la seconde passe non traités** : les échappements, l'indépendance du filet et Grep sur la
  racine sont décrits ci-dessus. Le verrou de `tools/secret_scan.py` et `tools/mutation_check.py` est fait (patch
  02). `stop_pytest.py` reste volontairement modifiable. `.claude/commands/audit.md` n'existe pas ; le skill est
  `.claude/skills/audit/SKILL.md`, non verrouillé, à trancher. Les « Décisions de validation » de la spec 003 ne
  reprennent pas encore les décisions de la relecture externe.

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
