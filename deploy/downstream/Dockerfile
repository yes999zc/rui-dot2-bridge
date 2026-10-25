# syntax=docker/dockerfile:1
#
# Downstream adapter layer for the official rui image (issue #7 Q1, plug recipe in
# docs/plug-test-kit.md). The rui core is NOT forked: this image only adds the dot2
# plug (adapter + its `rui-device/v1` manifest) and the two env vars that make the
# 0.3.x manifest-gated loader import it.
#
#   docker build -f deploy/downstream/Dockerfile -t rui-dot2:0.3.4 .
#
# Pin a concrete upstream tag/digest; never build from `latest` in production.
ARG RUI_IMAGE=ghcr.io/yes999zc/rui:0.3.4
FROM ${RUI_IMAGE}

# Package dir == manifest root: the module is `rui_dot2_bridge.adapter`, and rui's
# _module_plugin_roots() looks for plugin.yaml in module_file.parent, then .parent.parent.
# Keeping plugin.yaml + devices/ inside the package makes both places agree, and the
# manifest is what gates RUI_ADAPTER_PLUGINS (manifest-less entries are ignored).
COPY --chown=1000:1000 src/rui_dot2_bridge /opt/rui-plugins/rui_dot2_bridge

ENV PYTHONPATH=/opt/rui-plugins \
    RUI_ADAPTER_PLUGINS=dot2=rui_dot2_bridge.adapter:build \
    DOT2_SECRETS_DIR=/opt/rui-secrets
