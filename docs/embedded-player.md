# HTML player transport

The direct browser adapter prefers explicit audio controls. If absent, it can inspect an iframe using the profile's `player` contract: host/path patterns, named identities, HTML `element`, `identity_attributes`, `unprotected_attributes`, `media_attribute` and `poster_attribute`.

The HTML parser binds player attributes to URL identities. Unprotected HLS is resolved from the configured media attribute. Source artwork has priority over the matching poster. Missing artwork never invalidates otherwise valid audio. Public examples live in `test_profiles.py`; real profiles belong outside the repository.
