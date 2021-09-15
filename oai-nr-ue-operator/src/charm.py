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

from kubernetes_service import K8sServicePatch, PatchFailed

logger = logging.getLogger(__name__)

SCTP_PORT = 38412
HTTP1_PORT = 80
HTTP2_PORT = 9090


class OaiNrUeCharm(CharmBase):
    """Charm the service."""

    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        event_observer_mapping = {
            self.on.nr_ue_pebble_ready: self._on_oai_nr_ue_pebble_ready,
            self.on.tcpdump_pebble_ready: self._on_tcpdump_pebble_ready,
            self.on.install: self._on_install,
            self.on.config_changed: self._on_config_changed,
            self.on.gnb_relation_changed: self._update_service,
            self.on.gnb_relation_broken: self._update_service,
        }
        for event, observer in event_observer_mapping.items():
            self.framework.observe(event, observer)
        self._stored.set_default(
            gnb_host=None,
            gnb_port=None,
            gnb_api_version=None,
            _k8s_stateful_patched=False,
            _k8s_authed=False,
        )

    ####################################
    # Observers - Charm Events
    ####################################

    def _on_install(self, event):
        self._k8s_auth()
        self._patch_stateful_set()
        K8sServicePatch.set_ports(
            self.app.name,
            [
                ("s1c", 36412, 36412, "UDP"),
                ("s1u", 2152, 2152, "UDP"),
                ("x2c", 36422, 36422, "UDP"),
            ],
        )

    def _on_config_changed(self, event):
        self._update_tcpdump_service(event)

    ####################################
    # Observers - Pebble Events
    ####################################

    def _on_oai_nr_ue_pebble_ready(self, event):
        container = event.workload
        entrypoint = "/opt/oai-nr-ue/bin/entrypoint.sh"
        command = " ".join(
            [
                "/opt/oai-nr-ue/bin/nr-uesoftmodem.Rel15",
                "-O",
                "/opt/oai-nr-ue/etc/nr-ue-sim.conf",
            ]
        )
        pebble_layer = {
            "summary": "oai_nr_ue layer",
            "description": "pebble config layer for oai_nr_ue",
            "services": {
                "oai_nr_ue": {
                    "override": "replace",
                    "summary": "oai_nr_ue",
                    "command": f"{entrypoint} {command}",
                    "environment": {
                        "TZ": "Europe/Paris",
                        "FULL_IMSI": "208950000000031",
                        "FULL_KEY": "0C0A34601D4F07677303652C0462535B",
                        "OPC": "63bfa50ee6523365ff14c1f45f88737d",
                        "DNN": "oai",
                        "NSSAI_SST": "1",
                        "NSSAI_SD": "1",
                        "USE_ADDITIONAL_OPTIONS": "-E --sa --rfsim -r 106 --numerology 1 -C 3619200000 --nokrnmod",
                    },
                }
            },
        }
        try:
            container.add_layer("oai_nr_ue", pebble_layer, combine=True)
            self._update_service(event)
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()
            return

    def _on_tcpdump_pebble_ready(self, event):
        self._update_tcpdump_service(event)

    ####################################
    # Properties
    ####################################

    @property
    def is_gnb_ready(self):
        return self._stored.gnb_host

    @property
    def namespace(self) -> str:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace", "r") as f:
            return f.read().strip()

    @property
    def pod_ip(self) -> Optional[IPv4Address]:
        return IPv4Address(
            check_output(["unit-get", "private-address"]).decode().strip()
        )
    @property
    def container_name(self):
        return "nr-ue"

    @property
    def service_name(self):
        return "oai_nr_ue"

    @property
    def is_service_running(self):
        container = self.unit.get_container(self.container_name)
        return (
            self.service_name in container.get_plan().services
            and container.get_service(self.service_name).is_running()
        )

    ####################################
    # Utils - Services and configuration
    ####################################

    def _update_service(self, event):
        self._load_gnb_data()
        if self.is_gnb_ready:
            try:
                self._configure_service()
            except ConnectionError:
                logger.info("pebble socket not available, deferring config-changed")
                event.defer()
                return
            self._start_service(container_name="nr-ue", service_name="oai_nr_ue")
            self.unit.status = ActiveStatus()
        else:
            self._stop_service(container_name="nr-ue", service_name="oai_nr_ue")
            self.unit.status = BlockedStatus("need gnb relation")

    def _load_gnb_data(self):
        relation = self.framework.model.get_relation("gnb")
        if relation and relation.app in relation.data:
            relation_data = relation.data[relation.app]
            self._stored.gnb_host = relation_data.get("host")
            self._stored.gnb_port = relation_data.get("port")
            self._stored.gnb_api_version = relation_data.get("api-version")
        else:
            self._stored.gnb_host = None
            self._stored.gnb_port = None
            self._stored.gnb_api_version = None

    def _configure_service(self):
        container = self.unit.get_container("nr-ue")
        if self.service_name in container.get_plan().services:
            container.add_layer(
                "oai_nr_ue",
                {
                    "services": {
                        "oai_nr_ue": {
                            "override": "merge",
                            "environment": {
                                "RFSIMULATOR": self._stored.gnb_host,
                            },
                        }
                    },
                },
                combine=True,
            )

    def _start_service(self, container_name, service_name):
        container = self.unit.get_container(container_name)
        service_exists = service_name in container.get_plan().services
        is_running = container.get_service(service_name).is_running()

        if service_exists and not is_running:
            container.start(service_name)
            return True

    def _stop_service(self, container_name, service_name):
        container = self.unit.get_container(container_name)
        is_running = (
            service_name in container.get_plan().services
            and container.get_service(service_name).is_running()
        )
        if is_running:
            container.stop(service_name)

    ####################################
    # Utils - TCP Dump configuration
    ####################################

    def _update_tcpdump_service(self, event):
        try:
            self._configure_tcpdump_service()
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()
            return
        if self.config["start-tcpdump"]:
            self._start_service("tcpdump", "tcpdump")
        else:
            self._stop_service("tcpdump", "tcpdump")

    def _configure_tcpdump_service(self):
        container = self.unit.get_container("tcpdump")
        container.add_layer(
            "tcpdump",
            {
                "summary": "tcpdump layer",
                "description": "pebble config layer for tcpdump",
                "services": {
                    "tcpdump": {
                        "override": "replace",
                        "summary": "tcpdump",
                        "command": f"/usr/sbin/tcpdump -i any -w /pcap_{self.app.name}.pcap",
                        "environment": {
                            "DEBIAN_FRONTEND": "noninteractive",
                            "TZ": "Europe/Paris",
                        },
                    }
                },
            },
            combine=True,
        )

    ####################################
    # Utils - K8s authentication
    ###########################

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
    main(OaiNrUeCharm)
