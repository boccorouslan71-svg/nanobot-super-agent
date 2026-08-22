---
name: football-viral-news
description: Recherche les actualités football du jour, télécharge une vraie image du sujet trouvé, rédige un post Facebook viral au ton humain, le publie avec la photo sur la page, puis republie la publication en story. À la demande ou planifié chaque jour via l'outil cron.
metadata: {"nanobot":{"emoji":"⚽","requires":{"bins":["curl"]}}}
---

# Football Viral News

Pipeline complet : tendances du jour → sujet le plus viral → vraie image →
texte humain accrocheur → post Facebook avec photo → story.

## Pages disponibles

| Page | page_id |
|------|---------|
| Kalza Officiel (défaut pour le football) | `1129160526949038` |
| Maya Gold | `1192699313930831` |

Toute la publication passe par l'outil `composio_execute` (jamais d'appel
Graph API direct ni de token utilisateur).

## Étape 1 — Rechercher les tendances du jour

Fais 2 à 3 recherches complémentaires avec `web_search`, par exemple :

```
web_search(query="actualités football aujourd'hui <date du jour>")
web_search(query="football mercato transferts rumeurs aujourd'hui")
web_search(query="<club ou joueur en vogue> actualité")
```

Garde uniquement les infos **datées du jour ou de la veille** ; ignore tout
article de plus de 48 h.

## Étape 2 — Choisir LE sujet le plus viral

Classe les sujets trouvés avec ces critères (dans l'ordre) :

1. **Émotion forte** : polémique, coup de théâtre, record, humiliation,
   retour spectaculaire, déclaration choc.
2. **Star impliquée** : Messi, Ronaldo, Mbappé, joueurs de Ligue 1/Premier
   League très suivis en Afrique francophone.
3. **Débat possible** : le sujet doit diviser (« Qui a raison ? », « Le plus
   fort de l'histoire ? ») — un sujet qui fait commenter est meilleur qu'un
   sujet qui fait seulement liker.
4. **Fraîcheur** : publier avant les grandes pages concurrentes.

Choisis UN seul sujet. Si rien de viral n'émerge, dis-le franchement et ne
publie rien.

## Étape 3 — Trouver et télécharger la VRAIE image du sujet

**a) Extraire l'image depuis l'article source :**

```
web_fetch(url="<URL de l'article>")
```

Cherche dans le contenu la balise `og:image` (meta Open Graph) ou la première
URL d'image de l'article. C'est l'image qui illustre réellement le sujet.

**b) Télécharger et valider l'image (obligatoire avant publication) :**

```bash
exec(command="curl -sL --max-time 30 -o /tmp/fb_media.jpg '<URL_IMAGE>' && ls -la /tmp/fb_media.jpg")
exec(command="python3 -c \"print(open('/tmp/fb_media.jpg','rb').read(4))\"")
```

Validation :
- taille > 10 Ko et < 5 Mo (sinon l'upload Graph API échoue) ;
- octets magiques valides : `\xff\xd8\xff` = JPEG, `\x89PNG` = PNG ;
- si invalide ou trop petit → passer au fallback ci-dessous.

**c) Fallback si aucune image exploitable — Wikimedia Commons (libre de droits) :**

```bash
exec(command="curl -s 'https://commons.wikimedia.org/w/api.php?action=query&format=json&prop=imageinfo&iiprop=url|size&iiurlwidth=1080&generator=search&gsrsearch=<mots-cles du sujet>&gsrlimit=5&gsrnamespace=6'")
```

Prendre `thumburl` (redimensionnée 1080 px) d'un résultat dont `width >= 800`,
puis le télécharger comme en (b).

Ne publie JAMAIS une image que tu n'as pas réussi à télécharger et valider.

## Étape 4 — Rédiger le texte viral (ton 100 % humain)

Règles de style :

- **Accroche en première ligne** qui interpelle directement : « Vous avez vu
  ça ? 😳 », « Non mais il est sérieux lui ? », « Arrêtez tout. »
- **Ton conversationnel**, comme un fan qui parle à ses amis — pas un journaliste.
- **Opinion tranchée assumée** (mais jamais insultante ni diffamatoire).
- **Question finale obligatoire** pour provoquer les commentaires :
  « Et vous, vous en pensez quoi ? », « Team A ou Team B ? 👇 »
- **Emojis modérés** : 1 à 4 maximum, bien placés.
- **Court** : 2 à 4 phrases. Les longs pavés tuent l'engagement.
- Interdits : « En tant qu'intelligence artificielle… », tournures corporate
  (« Nous sommes ravis d'annoncer »), listes à puces, plus de 2 hashtags,
  faits inventés ou exagérés — reste fidèle à la source.
- Langue : français naturel (les anglicismes football OK : transfer, deal, top).

Exemple de transformation :

> ❌ Robotique : « Nous informons notre communauté que le joueur X a marqué un but lors du match d'hier soir. #Football #Sport »
>
> ✅ Humain/viral : « Il a fait ça à la 89e minute… 😳 Personne n'y croyait
> anymore et BOOM, frappe en pleine lucarne ! Vous avez vu ce match ou quoi ?
> 👇 »

## Étape 5 — Publier le post avec la photo

```
composio_execute(
    tool_slug="facebook_create_photo_post",
    arguments={
        "page_id": "<page_id>",
        "file_path": "/tmp/fb_media.jpg",
        "message": "<texte viral de l'étape 4>",
    },
)
```

(`url": "<URL publique>"` peut remplacer `file_path` si l'image est servie
en public.) Récupère le `post_id` renvoyé pour le rapport final.

## Étape 6 — Republier la publication en story

Immédiatement après, avec le même média :

```
composio_execute(
    tool_slug="facebook_create_story",
    arguments={
        "page_id": "<page_id>",
        "media_type": "photo",
        "file_path": "/tmp/fb_media.jpg",
    },
)
```

## Étape 7 — Rapport

Résume en une ligne à l'utilisateur : sujet choisi, lien de la source,
post_id publié, statut de la story. Ne colle jamais de token ni d'URL
interne dans le rapport.

## Planification quotidienne (cron)

Pour automatiser à heure fixe, enregistre une tâche récurrente :

```
cron(
    action="add",
    message="Exécute le skill football-viral-news sur la page Kalza Officiel "
            "(1129160526949038) : tendances football du jour, télécharge une "
            "vraie image du sujet, rédige un post viral au ton humain, publie-le "
            "avec sa photo puis republie-le en story. Fais le rapport ensuite.",
    cron_expr="0 17 * * *",
    tz="Africa/Douala",
)
```

Adapte `cron_expr` (heure/min) et `tz` à la demande de l'utilisateur.
Vérifie l'existant avec `cron(action="list")` avant d'en ajouter une nouvelle
pour éviter les doublons quotidiens.

## Anti-patterns

<rule>
**Ne pas publier sans image validée.** Un upload cassé = post moche visible
par tous les abonnés.
</rule>

<rule>
**Ne pas inventer.** Si la recherche ne donne rien de frais, ne force pas une
publication.
</rule>

<rule>
**Ne pas toucher aux deux pages en même temps** sauf demande explicite :
une seule page par run (Kalza par défaut).
</rule>
