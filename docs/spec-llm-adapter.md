# Spécification : couche LLM neutre, journal de coûts, évaluation (T1/T2)

## Objectif
Isoler tous les appels LLM derrière un adaptateur unique. Changer le modèle d'un agent
doit se réduire à une ligne de configuration plus un rejeu de l'évaluation.
Chaque appel est journalisé (modèle, tokens, coût). Un plafond de dépense LLM est appliqué hors LLM.

## Périmètre v0
- Fournisseur implémenté : Anthropic uniquement.
- Gemini est prévu mais non implémenté. Il ne sera ajouté que pour (a) la génération d'images et de vidéos
  et (b) la recherche web de l'agent Veille, dans une tâche séparée.
- Aucun autre fournisseur en v0.

## Arborescence attendue
- `llm/adapter` : une seule fonction d'entrée, utilisée par tous les agents (`call`). Pour les lots,
  `collect_batch` (appelée par le cron) récupère les résultats et `check_stale_batches` surveille les lots oubliés.
- `llm/providers/anthropic` : implémentation du fournisseur.
- `config/agents.yaml` : agent -> fournisseur, modèle, mode, `max_tokens` (obligatoire), température,
  outils autorisés (`allowed_tools`) ; plafonds de dépense.
- `config/prices.yaml` : tarifs par modèle, avec `verified_on` et `valid_until` (promotions).
- `evals/<agent>/` : cas d'évaluation au format YAML.
- `llm/evalrunner` : rejoue les cas sur un ou plusieurs modèles et produit un rapport.
- Clés d'API uniquement dans `.env` (jamais dans le dépôt). `.env.example` fourni.

## Entrées et sorties de l'adaptateur
Entrées :
- `agent` (veille, sourcing, boutique, contenu, marketing, sav)
- `shop_id` (nullable pour les appels globaux)
- `messages` (en mode `batch` : une liste de conversations, une par élément du lot)
- `schema` de sortie structurée (optionnel)
- `mode` : `sync` ou `batch` (par défaut : celui de l'agent dans `agents.yaml`)
- `cache_prefix` (optionnel) : partie stable du prompt à mettre en cache (playbook, checklists)
- `tools` (optionnel) : outils demandés, contrôlés par la règle 3
- `purpose` : `prod` (défaut) ou `eval` (appels du runner d'évaluation)

Sorties :
- contenu (texte ou objet validé par le schéma)
- `usage` : tokens en entrée, en sortie, lus depuis le cache, écrits dans le cache
- `cost_usd` calculé depuis `prices.yaml`
- `call_id`
- en mode `batch` : un identifiant de lot et le montant réservé ; les résultats arrivent via `collect_batch`.

## Configuration par défaut (hypothèse, à confirmer par l'évaluation)
| Agent | Modèle | Mode |
| --- | --- | --- |
| sourcing | claude-haiku-5-5 | sync |
| sav | claude-haiku-5-5 | sync |
| veille | claude-sonnet-5-5 | batch (cron de nuit) |
| boutique (fiches) | claude-sonnet-5-5 | batch si non urgent |
| marketing | claude-sonnet-5-5 | sync |
| contenu | claude-sonnet-5-5 | batch (brouillons) |

## Plafonds (dans `config/agents.yaml`)
- Production : 60 $/mois global, 45 $/mois par boutique (exceptions possibles par `shop_id`).
- Évaluation : 5 $/mois, plafond distinct appliqué aux appels `purpose: eval`.
  Les appels `eval` ne comptent pas dans les plafonds de production, et inversement.
- Alerte Telegram à 80 % de chaque plafond (une seule fois par mois et par plafond), refus à 100 %.

## Journal SQLite : table `llm_calls`
Champs : `call_id`, `ts`, `shop_id`, `agent`, `provider`, `model`, `mode`, `purpose`, `tokens_in`,
`tokens_out`, `tokens_cache_read`, `tokens_cache_write`, `tokens_in_estimated`, `tokens_out_estimated`,
`cost_estimated_usd`, `cost_usd`, `latency_ms`, `status`, `error`, `attempt`, `batch_id`, `dry_run`.
Aucune donnée client ni contenu de prompt dans cette table (uniquement des métriques).
Une vue agrège le coût par mois, par agent, par boutique et par usage (`prod` / `eval`).
Les migrations sont rejouables (table `schema_version`).

## Estimation avant appel
- Entrée estimée localement (caractères / 3,5), majorée de 20 %.
- Sortie estimée = `max_tokens` de l'agent (pire cas).
- Le palier tarifaire « > 100 000 tokens » se décide sur l'entrée majorée.
- Les tokens estimés et réels sont journalisés pour calibrer la marge.

## Règles (codées hors LLM)
1. Plafond mensuel de dépense LLM, global et par boutique, lu dans la config. Avant chaque appel,
   le coût estimé est ajouté au cumul du mois. Si le plafond serait dépassé, l'appel est refusé et une alerte Telegram part.
2. Pour un lot (`batch`), le coût maximal estimé est réservé dans le cumul mensuel à l'envoi (ligne `reserved`
   liée au lot). Un lot dont la réservation dépasserait un plafond n'est pas envoyé. À la collecte, la réservation
   est remplacée par le coût réel au tarif batch. Un lot non collecté après 24 h déclenche une alerte.
   La limite configurée chez le fournisseur peut être dépassée légèrement par un lot : elle ne sert pas de garde-fou.
3. Outils autorisés par agent : liste blanche `allowed_tools` dans `agents.yaml` (vide ou absente = aucun outil).
   Tout outil absent de la liste est refusé, y compris pour `veille`. Seconde couche, indépendante de la config :
   seul `veille` peut recevoir un outil web (détection par mots-clés). L'adaptateur refuse toute requête
   qui en fournit un à un autre agent.
4. Prompts de plus de 100 000 tokens : l'appel est journalisé avec une alerte, car le tarif de Haiku 5.5 est multiplié par 5 au-delà.
5. Un tarif absent ou périmé (`valid_until` dépassé) dans `prices.yaml` déclenche une alerte Telegram.
   L'appel continue avec le dernier tarif connu.
6. Nouvelles tentatives : 2 maximum, avec délai croissant. Chaque tentative est journalisée.
7. Journalisation : jamais de clé d'API ni d'adresse email dans les logs.

## Évaluation
- Format d'un cas : `id`, `input`, `expected` (critères vérifiables par code : champs requis, mots interdits,
  valeurs numériques) et, pour les sorties libres, une grille de critères notés 0/1.
- Les cas d'exemple écrits à la main portent `synthetic: true` ; le rapport les signale.
- Le rapport donne, par modèle : score, coût total, latence médiane, nombre d'échecs de format.
- Le runner ne change jamais la configuration : il produit un rapport, la décision reste humaine.
- Jeu de départ : 5 cas par agent, à compléter avec des cas réels au fil des runs (cible : 15 à 20).

## Critères de test (aucun appel réel au fournisseur : clients simulés)
- Un appel simulé écrit une ligne dans `llm_calls` avec un coût égal au calcul manuel (3 cas de tarifs, dont un en cache).
- Changer le modèle d'un agent dans `agents.yaml` ne demande aucune modification de code.
- Un dépassement de plafond global, puis un dépassement de plafond par boutique, refusent l'appel et déclenchent l'alerte.
- Une requête avec outil web pour un agent autre que `veille` est refusée.
- Un lot dont le coût maximal estimé dépasse le plafond n'est pas envoyé.
- Un tarif expiré déclenche l'alerte sans bloquer l'appel.
- L'ajout d'un second fournisseur (stub) ne modifie que `llm/providers` et `agents.yaml`.
- Le runner d'évaluation produit un rapport comparatif sur 2 modèles simulés.
- Un lot réservé puis collecté remplace la réservation par le coût réel (cumul exact après collecte).
- Deux lots successifs dont la somme des réservations dépasse le plafond : le second est refusé.
- Un appel `eval` ne fait pas bouger le cumul de production, et inversement.
- Une entrée à 90 000 tokens estimés (108 000 avec la marge de 20 %) est facturée au palier supérieur dans l'estimation.
- L'alerte à 80 % part une seule fois par mois et par plafond.
