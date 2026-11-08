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
  `effort` (`low` à `max`, `low` au départ ; null = défaut du modèle), outils autorisés (`allowed_tools`),
  `web_search_max_uses` (obligatoire si `web_search` est autorisé) ; plafonds de dépense ;
  `estimation.search_tokens_estimate` (tokens d'entrée par recherche, 4 000 au départ).
- `config/prices.yaml` : tarifs par modèle, avec `verified_on` et `valid_until` (promotions) ; `server_tools` :
  frais à l'unité par fournisseur (`web_search_per_1000: 10`, sans remise batch).
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
- `usage` : tokens en entrée (hors cache), en sortie, lus depuis le cache, écrits dans le cache, recherches web
  (`web_search_requests`). Taille du prompt au sens tarifaire = entrée + cache lu + cache écrit.
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
`cost_estimated_usd`, `cost_usd`, `latency_ms`, `status`, `error`, `attempt`, `batch_id`, `dry_run`,
`billing_month` (migration 003), `stop_reason`, `web_search_requests` (migration 004).
Aucune donnée client ni contenu de prompt dans cette table (uniquement des métriques).
Statuts (documentés dans la table `llm_call_statuses`, migration 002) : `pending` (appel synchrone en cours,
coût estimé réservé), `ok`, `error`, `format_error`, `refused`, `reserved` (lot envoyé, coût maximal réservé),
`released` (réservation de lot remplacée par le réel), `abandoned` (réservation `pending` expirée, `cost_usd = 0`),
`incomplete` (migration 004 : réponse tronquée ou interrompue, coût réel). `refused` couvre le refus d'un garde-fou
(`cost_usd = 0`, `stop_reason` NULL) et, depuis la migration 004, le refus du modèle (`stop_reason = refusal`, coût réel).
Mois de facturation : `billing_month` (`YYYY-MM`, UTC) est rempli à l'insertion avec le mois de `ts`, sauf pour
les lignes réelles d'un lot, qui recopient celui de leur réservation (mois d'envoi). Les cumuls mensuels
(`month_spend`) et la vue filtrent sur ce champ, pas sur `ts`, avec le même repli :
`COALESCE(billing_month, substr(ts, 1, 7))`. Une ligne écrite sans `billing_month` (SQL brut, outil externe)
est donc toujours comptée dans le plafond du mois de son `ts`.
Une vue agrège le coût par mois de facturation, par agent, par boutique et par usage (`prod` / `eval`).
Les migrations sont rejouables (table `schema_version`) ; une migration publiée n'est jamais modifiée.

## Estimation avant appel
- Entrée estimée localement (caractères / 3,5), majorée de 20 %.
- Sortie estimée = `max_tokens` de l'agent (pire cas).
- Recherches web (si un outil web est fourni) : pire cas de `web_search_max_uses` recherches, chacune ajoutant
  `search_tokens_estimate` tokens d'entrée et 0,01 $ de frais (non remisés en lot). L'adaptateur impose `max_uses`
  à l'outil : l'appelant ne peut pas le relever.
- Le palier tarifaire « > 100 000 tokens » se décide sur l'entrée majorée (recherches comprises).
- Les tokens estimés et réels sont journalisés pour calibrer la marge.

## Règles (codées hors LLM)
1. Plafond mensuel de dépense LLM, global et par boutique, lu dans la config. Avant chaque appel,
   le coût estimé est ajouté au cumul du mois. Si le plafond serait dépassé, l'appel est refusé et une alerte Telegram part.
   Le contrôle et la réservation (ligne `pending`) sont faits dans une même transaction `BEGIN IMMEDIATE`.
   Expiration : une ligne `pending` plus vieille que `reservations.pending_ttl_minutes` (`agents.yaml`, défaut 15)
   est considérée comme abandonnée (processus tué avant d'écrire le résultat). À l'ouverture de chaque transaction
   de réservation (sync et lot), elle passe en `abandoned` avec `cost_usd = 0`, l'expiration est journalisée
   (champ `error` de la ligne et log applicatif) et une alerte Telegram part, une seule fois par jour.
   Si l'appel finit malgré tout après l'expiration, sa ligne reprend le statut et le coût réels.
2. Pour un lot (`batch`), le coût maximal estimé est réservé dans le cumul mensuel à l'envoi (ligne `reserved`
   liée au lot). Un lot dont la réservation dépasserait un plafond n'est pas envoyé. À la collecte, la réservation
   est remplacée par le coût réel au tarif batch. Un lot non collecté après 24 h déclenche une alerte.
   Identifiant provisoire : la ligne `reserved` est écrite, dans la transaction de réservation, avec
   `batch_id = pending-<call_id>` ; il est remplacé par l'identifiant du fournisseur après l'envoi. Si le processus
   meurt entre les deux, `check_stale_batches` signale aussi ces réservations après 24 h (lot peut-être envoyé et
   facturé : vérification manuelle chez le fournisseur ; la réservation reste comptée jusque-là).
   Mois de facturation : les lignes réelles d'un lot sont imputées au mois d'envoi (`billing_month` de la
   réservation), même si la collecte a lieu le mois suivant.
   La limite configurée chez le fournisseur peut être dépassée légèrement par un lot : elle ne sert pas de garde-fou.
3. Outils autorisés par agent : liste blanche `allowed_tools` dans `agents.yaml` (vide ou absente = aucun outil).
   Tout outil absent de la liste est refusé, y compris pour `veille`. Seconde couche, indépendante de la config :
   seul `veille` peut recevoir un outil web (détection par mots-clés). L'adaptateur refuse toute requête
   qui en fournit un à un autre agent.
4. Prompts de plus de 100 000 tokens : l'appel est journalisé avec une alerte, car le tarif de Haiku 5.5 est multiplié par 5 au-delà.
5. Tarifs dans `prices.yaml` (l'appel n'est jamais bloqué, une alerte Telegram part dans les deux cas) :
   - modèle absent de `prices.yaml` = tarif le plus cher de la table + alerte ;
   - tarif expiré (`valid_until` dépassé) = dernier tarif connu + alerte.
6. Nouvelles tentatives : 2 maximum, avec délai croissant. Chaque tentative est journalisée.
7. Journalisation : jamais de clé d'API ni d'adresse email dans les logs.
8. Complétion : seuls `end_turn` et `stop_sequence` sont un succès. `max_tokens`, `model_context_window_exceeded`,
   `pause_turn` (et tout motif inconnu ou absent) donnent `incomplete` et `LLMIncompleteError` ; `refusal` donne
   `refused` et `LLMRefusedError`, avec alerte Telegram une fois par jour et par agent. Aucune nouvelle tentative
   automatique, le coût réel est toujours journalisé, aucun contenu n'est renvoyé. En lot, chaque élément suit la
   même règle (`content = None`). Le schéma n'est validé que sur une réponse complète.

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
- L'ajout d'un second fournisseur (stub) ne modifie que `llm/providers` et `agents.yaml`. Contrôle permanent
  (AST) : `llm/adapter.py` et `llm/guards.py` n'importent aucun fournisseur ni le SDK (seul `llm.providers.base`).
- Chaque motif d'arrêt est testé ; coût et estimation des recherches web ; réservation d'un lot veille avec recherches.
- Le runner d'évaluation produit un rapport comparatif sur 2 modèles simulés.
- Un lot réservé puis collecté remplace la réservation par le coût réel (cumul exact après collecte).
- Deux lots successifs dont la somme des réservations dépasse le plafond : le second est refusé.
- Un appel `eval` ne fait pas bouger le cumul de production, et inversement.
- Une entrée à 90 000 tokens estimés (108 000 avec la marge de 20 %) est facturée au palier supérieur dans l'estimation.
- L'alerte à 80 % part une seule fois par mois et par plafond.
- Une ligne `pending` de 3 h est libérée (`abandoned`, coût 0) ; une de 5 min ne l'est pas ; l'alerte part une fois par jour.
- Plantage simulé entre l'envoi d'un lot et l'écriture de son identifiant : l'alerte de lot oublié part après 24 h.
- Un lot envoyé le 30 et collecté le 2 du mois suivant est imputé au mois d'envoi.
