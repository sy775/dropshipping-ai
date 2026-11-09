# Système dropshipping

Système d'agents IA qui crée et exploite des boutiques de niche en France (une boutique dédiée par niche validée), avec des fournisseurs UE. Le plan complet est dans `docs/business-plan.md` : à lire quand une décision d'architecture est en jeu, ne pas l'importer.

## Environnement
- Dépôt local : `D:/Projects/Dropshipping AI` (Windows, le chemin contient une espace : toujours entre guillemets dans les commandes).
- Python 3.12, SQLite (journal), pytest.
- Déploiement : serveur Linux Hetzner, tâches planifiées par cron.
- LLM : API Anthropic. Haiku pour le volume (sourcing, SAV), Sonnet pour le jugement (veille, fiches, pub, playbook).

## Structure
- `agents/` : veille (seul accès web), sourcing, boutique, contenu, marketing, sav
- `risk/` : garde-fous codés hors LLM, non contournables
- `journal/` : SQLite, une ligne par action, avec l'identifiant de la boutique
- `config/` : réglages YAML ; schéma JSON de la « spécification de niche » à créer dans `config/niche_spec.schema.json` (pas encore écrit)
- `private/niches/` : fiches de niche (ignoré par git, jamais commité)
- `connectors/` : Shopify Admin API, fournisseurs, Meta/TikTok Ads, tous avec mode dry-run
- `docs/specs/NNN-titre.md` : spécifications de développement (gabarit `_template.md`)
- `docs/reports/NNN-titre.md` : rapports de fin de spec, commités avec le code
- `docs/` : business plan (local, ignoré par git) et procédures
- `tools/` : scan de secrets, contrôle de mutations, audit ; `tools/hooks/` : hooks Claude Code
- `tests/`

## Règles
- Dry-run par défaut : aucune action réelle (pub, commande, publication) avant test en dry-run.
- Les garde-fous vivent dans le code et dans les tests, jamais seulement dans un prompt : plafond de pub par boutique et global, coupure si ROAS trop bas, marge minimale, fournisseurs en liste blanche.
- Les agents qui touchent à l'argent ou aux clients n'ont pas accès au web.
- L'agent contenu prépare des brouillons, il ne publie pas.
- Secrets : jamais dans le code ni dans git, uniquement en variables d'environnement (`.env` ignoré par git).
- Je dispose de 3 à 5 h par semaine : découpe en livrables courts, testables en une session.
- Plan d'abord pour toute tâche de plus de ~50 lignes : propose, attends ma validation. Petits commits, tests à chaque module.
- Après chaque tâche : 3 lignes sur ce qui a été fait et comment le tester.
- Si une instruction est contradictoire, ambiguë ou fausse, le dire AVANT d'exécuter.

## Processus de développement
1. Spec : `/spec NNN-titre` + texte collé → `docs/specs/NNN-titre.md`, branche `spec/NNN-titre` depuis `origin/main` à jour.
2. Plan : proposé à partir de CLAUDE.md et de la spec, puis arrêt.
3. Validation : par Younes, explicite. Aucun code avant.
4. Implémentation : petits commits testés sur la branche `spec/NNN-titre`.
5. `/audit` : pytest, `tools/secret_scan.py`, `tools/mutation_check.py`, puis sous-agent `critic` ; point bloquant = test rouge commité seul, puis correction dans un commit séparé.
6. Rapport : `docs/reports/NNN-titre.md`, commité avec le code.
7. Push de la branche `spec/NNN-titre` uniquement.
- Jamais de commit direct sur main, jamais de push vers main, jamais de fusion : la fusion reste à Younes.
- Hooks (`.claude/settings.json`, scripts dans `tools/hooks/`, lancés par `python -I`) : fichiers verrouillés et
  `.env*` protégés (écriture par outil, mention dans une commande shell, lecture de `.env*`), seul
  `git push [-u] origin spec/<nom>` permis, pytest en fin de tour.
- Fichiers et dossiers verrouillés : liste unique `tools/locked_paths.txt` (verrouillée elle-même), lue par le hook et
  par le filet de `tools/audit.py` et de la CI. Jamais modifiés par Claude. Si une spec doit en modifier un :
  s'arrêter, écrire le diff exact dans le rapport (patch) ; Younes l'applique lui-même sur une branche `human/*`.
- Catalogue `tools/mutations.yaml` : ajout seul, en fin de fichier. Le filet échoue si une ligne présente sur
  origin/main est modifiée ou supprimée : modifier une entrée existante (`old`, `new`, `file`) revient à Younes.
