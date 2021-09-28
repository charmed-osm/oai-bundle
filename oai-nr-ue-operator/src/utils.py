# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import os
import time
from typing import List, Set, Tuple, Optional

import kubernetes
from ops.charm import CharmBase
from ops.model import MaintenanceStatus
from ops.pebble import ConnectionError
from ops.framework import StoredState
from ipaddress import IPv4Address
import subprocess


class PatchFailed(RuntimeError):
    """Patching the kubernetes service failed."""


class K8sServicePatch:
    """A utility for patching the Kubernetes service set up by Juju.
    Attributes:
            namespace_file (str): path to the k8s namespace file in the charm container
    """

    namespace_file = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"

    @staticmethod
    def namespace() -> str:
        """Read the Kubernetes namespace we're deployed in from the mounted service token.
        Returns:
            str: The current Kubernetes namespace
        """
        with open(K8sServicePatch.namespace_file, "r") as f:
            return f.read().strip()

    @staticmethod
    def _k8s_service(
        app: str, service_ports: List[Tuple[str, int, int, str]]
    ) -> kubernetes.client.V1Service:
        """Property accessor to return a valid Kubernetes Service representation for Alertmanager.
        Args:
            app: app name
            service_ports: a list of tuples (name, port, target_port) for every service port.
        Returns:
            kubernetes.client.V1Service: A Kubernetes Service with correctly annotated metadata and
            ports.
        """
        ports = [
            kubernetes.client.V1ServicePort(
                name=port[0], port=port[1], target_port=port[2], protocol=port[3]
            )
            for port in service_ports
        ]

        ns = K8sServicePatch.namespace()
        return kubernetes.client.V1Service(
            api_version="v1",
            metadata=kubernetes.client.V1ObjectMeta(
                namespace=ns,
                name=app,
                labels={"app.kubernetes.io/name": app},
            ),
            spec=kubernetes.client.V1ServiceSpec(
                ports=ports,
                selector={"app.kubernetes.io/name": app},
            ),
        )

    @staticmethod
    def set_ports(app: str, service_ports: List[Tuple[str, int, int, str]]):
        """Patch the Kubernetes service created by Juju to map the correct port.
        Currently, Juju uses port 65535 for all endpoints. This can be observed via:
            kubectl describe services -n <model_name> | grep Port -C 2
        At runtime, pebble watches which ports are bound and we need to patch the gap for pebble
        not telling Juju to fix the K8S Service definition.
        Typical usage example from within charm code (e.g. on_install):
            service_ports = [("my-app-api", 9093, 9093), ("my-app-ha", 9094, 9094)]
            K8sServicePatch.set_ports(self.app.name, service_ports)
        Args:
            app: app name
            service_ports: a list of tuples (name, port, target_port) for every service port.
        Raises:
            PatchFailed: if patching fails.
        """
        # First ensure we're authenticated with the Kubernetes API

        ns = K8sServicePatch.namespace()
        # Set up a Kubernetes client
        api = kubernetes.client.CoreV1Api(kubernetes.client.ApiClient())
        try:
            # Delete the existing service so we can redefine with correct ports
            # I don't think you can issue a patch that *replaces* the existing ports,
            # only append
            api.delete_namespaced_service(name=app, namespace=ns)
            # Recreate the service with the correct ports for the application
            api.create_namespaced_service(
                namespace=ns, body=K8sServicePatch._k8s_service(app, service_ports)
            )
        except kubernetes.client.exceptions.ApiException as e:
            raise PatchFailed("Failed to patch k8s service: {}".format(e))


logger = logging.getLogger(__name__)


class OaiCharm(CharmBase):
    """Oai Base Charm."""

    _stored = StoredState()

    def __init__(
        self,
        *args,
        tcpdump: bool = False,
        ports=None,
        privileged: bool = False,
        container_name=None,
        service_name,
    ):
        super().__init__(*args)

        self.ports = ports
        self.privileged = privileged
        self.container_name = container_name
        self.service_name = service_name

        event_mapping = {
            self.on.install: self._on_install,
        }
        if tcpdump:
            event_mapping[self.on.tcpdump_pebble_ready] = self._on_tcpdump_pebble_ready
        for event, observer in event_mapping.items():
            self.framework.observe(event, observer)

        self._stored.set_default(
            _k8s_stateful_patched=False,
            _k8s_authed=False,
        )

    def _on_install(self, _=None):
        if not self._stored._k8s_authed:
            kubernetes.config.load_incluster_config()
            self._stored._k8s_authed = True
        if not self._stored._k8s_authed:
            kubernetes.config.load_incluster_config()
            self._stored._k8s_authed = True
        if self.privileged:
            self._patch_stateful_set()
        K8sServicePatch.set_ports(self.app.name, self.ports)

    def _on_tcpdump_pebble_ready(self, event):
        self.update_tcpdump_service(event)

    def update_tcpdump_service(self, event):
        try:
            self._configure_tcpdump_service()
            if (
                self.config["start-tcpdump"]
                and self.service_exists("tcpdump", "tcpdump")
                and not self.is_service_running("tcpdump", "tcpdump")
            ):
                self.start_service("tcpdump", "tcpdump")
            elif (
                not self.config["start-tcpdump"]
                and self.service_exists("tcpdump", "tcpdump")
                and self.is_service_running("tcpdump", "tcpdump")
            ):
                self.stop_service("tcpdump", "tcpdump")
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()

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

    def start_service(self, container_name=None, service_name=None):
        if not container_name:
            container_name = self.container_name
        if not service_name:
            service_name = self.service_name
        container = self.unit.get_container(container_name)
        logger.info(f"{container.get_plan()}")
        container.start(service_name)

    def stop_service(self, container_name=None, service_name=None):
        if not container_name:
            container_name = self.container_name
        if not service_name:
            service_name = self.service_name
        container = self.unit.get_container(container_name)
        container.stop(service_name)

    def is_service_running(self, container_name=None, service_name=None):
        if not container_name:
            container_name = self.container_name
        if not service_name:
            service_name = self.service_name
        container = self.unit.get_container(container_name)
        is_running = (
            service_name in container.get_plan().services
            and container.get_service(service_name).is_running()
        )
        logger.info(f"container {self.container_name} is running: {is_running}")
        return is_running

    def service_exists(self, container_name=None, service_name=None):
        if not container_name:
            container_name = self.container_name
        if not service_name:
            service_name = self.service_name
        container = self.unit.get_container(container_name)
        service_exists = service_name in container.get_plan().services
        logger.info(f"service {service_name} exists: {service_exists}")
        return service_exists

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

    @property
    def namespace(self) -> str:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace", "r") as f:
            return f.read().strip()

    @property
    def pod_ip(self) -> Optional[IPv4Address]:
        return IPv4Address(
            subprocess.check_output(["unit-get", "private-address"]).decode().strip()
        )

    def search_logs(
        self, logs: Set[str] = {}, subsets_in_line: Set[str] = {}, wait: bool = False
    ) -> bool:
        """
        Search list of logs in the container and service

        :param: logs: List of logs to be found
        :param: wait: Bool to wait until those logs are found
        """
        if logs and subsets_in_line:
            raise Exception("logs and subsets_in_line cannot both be defined")
        elif not logs and not subsets_in_line:
            raise Exception("logs or subsets_in_line must be defined")

        found_logs = set()
        os.environ[
            "PEBBLE_SOCKET"
        ] = f"/charm/containers/{self.container_name}/pebble.socket"
        p = subprocess.Popen(
            f'/charm/bin/pebble logs {self.service_name} {"-f" if wait else ""} -n all',
            stdout=subprocess.PIPE,
            shell=True,
            encoding="utf-8",
        )
        all_logs_found = False
        for line in p.stdout:
            if logs:
                for log in logs:
                    if log in line:
                        found_logs.add(log)
                        logger.info(f"{log} log found")
                        break

                if all(log in found_logs for log in logs):
                    all_logs_found = True
                    logger.info("all logs found")
                    break
            else:
                if all(subset in line for subset in subsets_in_line):
                    all_logs_found = True
                    logger.info("subset of strings found")
                    break
        p.kill()
        return all_logs_found
