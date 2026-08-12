# SearXNG with our settings.yml baked in, for hosts that do not hand a bind
# mount the repository's own files.
#
# Coolify parses `volumes: ./searxng:/etc/searxng` out of the compose file and
# turns it into a managed storage: it creates an *empty* directory on the host
# and mounts that. The repository's settings.yml never reaches the container, so
# SearXNG boots on its stock defaults — where `json` is not in `search.formats`
# and every API query comes back 403. Copying the file into the image removes
# the bind mount, and with it the failure mode.
FROM searxng/searxng:latest

COPY settings.yml /etc/searxng/settings.yml
