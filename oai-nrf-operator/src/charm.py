#!/usr/bin/env python3
# Copyright 2021 David Garcia
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm the service.

Refer to the following post for a quick-start guide that will help you
develop a new k8s charm using the Operator Framework:

    https://discourse.charmhub.io/t/4208
"""

from ipaddress import IPv4Address
import logging
from subprocess import check_output
from typing import Optional
import time

from kubernetes import kubernetes
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus
from ops.pebble import ConnectionError


logger = logging.getLogger(__name__)

SCTP_PORT = 38412
HTTP1_PORT = 80
HTTP2_PORT = 9090


class OaiNrfCharm(CharmBase):
    """Charm the service."""

    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        event_observer_mapping = {
            self.on.nrf_pebble_ready: self._on_oai_nrf_pebble_ready,
            self.on.tcpdump_pebble_ready: self._on_tcpdump_pebble_ready,
            self.on.install: self._on_install,
            self.on.config_changed: self._on_config_changed,
            self.on.nrf_relation_joined: self._provide_service_info,
        }
        for event, observer in event_observer_mapping.items():
            self.framework.observe(event, observer)
        self._stored.set_default(
            _k8s_stateful_patched=False,
            _k8s_authed=False,
        )

    ####################################
    # Observers - Relation Events
    ####################################

    def _provide_service_info(self, event):
        if not self.unit.is_leader():
            return
        pod_ip = self.pod_ip
        if pod_ip:
            event.relation.data[self.app]["host"] = str(pod_ip)
            event.relation.data[self.app]["port"] = str(HTTP1_PORT)
            event.relation.data[self.app]["api-version"] = "v1"
        else:
            event.defer()

    ####################################
    # Observers - Charm Events
    ####################################

    def _on_install(self, _):
        self._k8s_auth()
        self._patch_stateful_set()

    def _on_config_changed(self, _):
        if self.config["start-tcpdump"]:
            self._start_service("tcpdump", "tcpdump")
        else:
            self._stop_service("tcpdump", "tcpdump")

    ####################################
    # Observers - Pebble Events
    ####################################

    def _on_oai_nrf_pebble_ready(self, event):
        container = event.workload
        entrypoint = "/bin/bash /openair-nrf/bin/entrypoint.sh"
        command = " ".join(
            ["/openair-nrf/bin/oai_nrf", "-c", "/openair-nrf/etc/nrf.conf", "-o"]
        )
        pebble_layer = {
            "summary": "oai_nrf layer",
            "description": "pebble config layer for oai_nrf",
            "services": {
                "oai_nrf": {
                    "override": "replace",
                    "summary": "oai_nrf",
                    "command": f"{entrypoint} {command}",
                    "environment": {
                        "DEBIAN_FRONTEND": "noninteractive",
                        "TZ": "Europe/Paris",
                        "INSTANCE": "0",
                        "PID_DIRECTORY": "/var/run",
                        "NRF_INTERFACE_NAME_FOR_SBI": "eth0",
                        "NRF_INTERFACE_PORT_FOR_SBI": "80",
                        "NRF_INTERFACE_HTTP2_PORT_FOR_SBI": "9090",
                        "NRF_API_VERSION": "v1",
                    },
                }
            },
        }
        try:
            container.add_layer("oai_nrf", pebble_layer, combine=True)
            self._update_service(event)
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()
            return

    def _on_tcpdump_pebble_ready(self, event):
        container = event.workload
        command = f"/usr/sbin/tcpdump -i any -w /pcap_{self.app.name}.pcap"
        pebble_layer = {
            "summary": "tcpdump layer",
            "description": "pebble config layer for tcpdump",
            "services": {
                "tcpdump": {
                    "override": "replace",
                    "summary": "tcpdump",
                    "command": command,
                    "environment": {
                        "DEBIAN_FRONTEND": "noninteractive",
                        "TZ": "Europe/Paris",
                    },
                }
            },
        }
        try:
            container.add_layer("tcpdump", pebble_layer, combine=True)
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()
            return

    ####################################
    # Properties
    ####################################

    @property
    def namespace(self) -> str:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace", "r") as f:
            return f.read().strip()

    @property
    def pod_ip(self) -> Optional[IPv4Address]:
        return IPv4Address(
            check_output(["unit-get", "private-address"]).decode().strip()
        )

    ####################################
    # Utils - Services and configuration
    ####################################

    def _update_service(self, event):
        self._start_service(container_name="nrf", service_name="oai_nrf")
        self.unit.status = ActiveStatus()

    def _start_service(self, container_name, service_name):
        container = self.unit.get_container(container_name)
        is_running = (
            service_name in container.get_plan().services
            and container.get_service(service_name).is_running()
        )
        if not is_running:
            container.start(service_name)

    def _stop_service(self, container_name, service_name):
        container = self.unit.get_container(container_name)
        is_running = (
            service_name in container.get_plan().services
            and container.get_service(service_name).is_running()
        )
        if is_running:
            container.stop(service_name)

    ####################################
    # Utils - K8s authentication
    ####################################

    def _k8s_auth(self) -> bool:
        """Authenticate to kubernetes."""
        if self._stored._k8s_authed:
            return True
        kubernetes.config.load_incluster_config()
        self._stored._k8s_authed = True

    def _patch_stateful_set(self) -> None:
        """Patch the StatefulSet to include specific ServiceAccount and Secret mounts"""
        if self._stored._k8s_stateful_patched:
            return

        # Get an API client
        api = kubernetes.client.AppsV1Api(kubernetes.client.ApiClient())
        for attempt in range(5):
            try:
                self.unit.status = MaintenanceStatus(
                    f"patching StatefulSet for additional k8s permissions. Attempt {attempt+1}/5"
                )
                s = api.read_namespaced_stateful_set(
                    name=self.app.name, namespace=self.namespace
                )
                # Add the required security context to the container spec
                s.spec.template.spec.containers[1].security_context.privileged = True

                # Patch the StatefulSet with our modified object
                api.patch_namespaced_stateful_set(
                    name=self.app.name, namespace=self.namespace, body=s
                )
                logger.info(
                    "Patched StatefulSet to include additional volumes and mounts"
                )
                self._stored._k8s_stateful_patched = True
                return
            except Exception as e:
                self.unit.status = MaintenanceStatus(
                    "failed patching StatefulSet... Retrying in 10 seconds"
                )
                time.sleep(5)


if __name__ == "__main__":
    main(OaiNrfCharm, use_juju_for_storage=True)
