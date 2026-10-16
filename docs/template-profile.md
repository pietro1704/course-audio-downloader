# Declarative site profiles

A private JSON profile supplies the authenticated origin and the website contract. No site domain, product ID or course title belongs in application code.

```json
{
  "schema": "course-site-profile/v1",
  "mode": "template-hls",
  "origin": "https://lessons.example.test",
  "lesson_path_pattern": "/lesson/[0-9]+",
  "login_paths": ["/login"],
  "title_suffix_pattern": " - Example Academy$",
  "active_selector": ".active-lessons a[href]",
  "identity_expressions": {"collection": "window.collectionId", "lesson": "window.lessonId"},
  "player": {
    "host_pattern": "player.example.test",
    "path_pattern": "/embed/(?P<video>[a-z0-9-]+)",
    "manifest_url": "https://media.example.test/{video}/playlist.m3u8",
    "thumbnail_url": "https://media.example.test/{video}/thumbnail.jpg"
  }
}
```

Each lesson provides matching `page_identity` values and optional `page_title` when its document heading differs from the literal output title. A resolver requires three stable observations, matching active labels, URL and configured identities. A reused player across different lessons is rejected. Login redirects and timeouts never publish media. Templates may additionally specify `config_url`, named host captures, query fields, allowed query keys and field patterns. Encrypted or unbounded playlists are rejected.

Keep these profiles local; never include session tokens or signed media URLs. See the synthetic fixtures in `test_profiles.py` for complete transport examples.
