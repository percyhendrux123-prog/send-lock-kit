ARG DOOR_BASE_IMAGE=python:3.12-slim
FROM ${DOOR_BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /kit
# Explicit files only; context allowlist excludes credentials and future files.
COPY deploy/one_door/sender.py deploy/one_door/gateway.py deploy/one_door/owner.py deploy/one_door/fake_services.py deploy/one_door/init_demo.py deploy/one_door/container_probe.py /kit/
COPY deploy/one_door/config.yaml deploy/one_door/axiom-owner.json deploy/one_door/egress.json /kit/
COPY plugins/send-lock/__init__.py plugins/send-lock/plugin.yaml /plugin/
USER 10001:10002
CMD ["sleep", "infinity"]
