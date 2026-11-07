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
- `specs/` : schéma JSON de la « spécification de niche »
- `connectors/` : Shopify Admin API, fournisseurs, Meta/TikTok Ads, tous avec mode dry-run
- `docs/` : business plan et spécifications produites dans le chat
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
