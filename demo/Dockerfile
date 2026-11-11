FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/kit
# Reviewed filenames at BOTH context and COPY boundaries; no directory copies.
COPY deploy/install_vps.sh deploy/hermes-gateway.service deploy/
COPY scripts/alert_health.py scripts/clickup.py scripts/for_axiom.py scripts/gmail_auth_readonly.py scripts/rundown.py scripts/rundown_adapters.py scripts/selfsend.py scripts/stall_watch.py scripts/work_gmail_watch.py scripts/
COPY plugins/send-lock/__init__.py plugins/send-lock/plugin.yaml plugins/send-lock/
RUN bash deploy/install_vps.sh --non-interactive --container --user hermes \
    && rm -rf /var/lib/apt/lists/*
COPY --chown=hermes:hermes tests/test_clickup.py tests/test_demo.py tests/test_demo_safety.py tests/test_install_vps.py tests/test_rundown.py tests/test_rundown_adapters.py tests/test_send_lock.py tests/test_stall_watch.py tests/
COPY --chown=hermes:hermes demo/Dockerfile demo/run.sh demo/verify.sh demo/README.md demo/
COPY --chown=hermes:hermes demo/fixtures/businesses.json demo/fixtures/calendar.json demo/fixtures/mail.json demo/fixtures/tasks.json demo/fixtures/people.json demo/fixtures/
COPY --chown=hermes:hermes docs/INSTALL-VPS.md docs/MIGRATE-VPS-TO-MAC-MINI.md docs/
COPY README.md .dockerignore ./
# Tests create only synthetic temporary state. No systemd, bot or provider login.
RUN chown hermes:hermes /opt/kit
USER hermes
CMD ["bash", "demo/verify.sh", "docker-offline"]
