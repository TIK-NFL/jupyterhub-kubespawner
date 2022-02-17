import asyncio
import json
import os
import string

import escapism
import kubernetes.config
from jupyterhub.proxy import Proxy
from jupyterhub.utils import exponential_backoff
from kubernetes_asyncio import client
from traitlets import Unicode

from .clients import load_config
from .clients import shared_client
from .objects import make_ingress
from .reflector import ResourceReflector
from .utils import generate_hashed_slug


class IngressReflector(ResourceReflector):
    kind = 'ingresses'
    api_group_name = 'ExtensionsV1beta1Api'

    @property
    def ingresses(self):
        return self.resources


class ServiceReflector(ResourceReflector):
    kind = 'services'

    @property
    def services(self):
        return self.resources


class EndpointsReflector(ResourceReflector):
    kind = 'endpoints'

    @property
    def endpoints(self):
        return self.resources


class KubeIngressProxy(Proxy):
    """
    As the KubeIngressProxy class now depends on an asyncio-based
    Kubernetes client, it is no longer sufficient to simply instantiate
    an instance of the class with `__init__`.  Instance configuration is
    required, and that depends on async functions, which cannot appear in
    `__init__`.

    Therefore, immediately after requesting a new instance, the requestor
    should await the instance's `initialize_resources` method
    to complete configuration of the instance.
    """

    namespace = Unicode(
        config=True,
        help="""
        Kubernetes namespace to spawn ingresses for single-user servers in.

        If running inside a kubernetes cluster with service accounts enabled,
        defaults to the current namespace. If not, defaults to 'default'
        """,
    )

    def _namespace_default(self):
        """
        Set namespace default to current namespace if running in a k8s cluster

        If not in a k8s cluster with service accounts enabled, default to
        'default'
        """
        ns_path = '/var/run/secrets/kubernetes.io/serviceaccount/namespace'
        if os.path.exists(ns_path):
            with open(ns_path) as f:
                return f.read().strip()
        return 'default'

    component_label = Unicode(
        'singleuser-server',
        config=True,
        help="""
        The component label used to tag the user pods. This can be used to override
        the spawner behavior when dealing with multiple hub instances in the same
        namespace. Usually helpful for CI workflows.
        """,
    )

    k8s_api_ssl_ca_cert = Unicode(
        "",
        config=True,
        help="""
        Location (absolute filepath) for CA certs of the k8s API server.

        Typically this is unnecessary, CA certs are picked up by
        config.load_incluster_config() or config.load_kube_config.

        In rare non-standard cases, such as using custom intermediate CA
        for your cluster, you may need to mount root CA's elsewhere in
        your Pod/Container and point this variable to that filepath
        """,
    )

    k8s_api_host = Unicode(
        "",
        config=True,
        help="""
        Full host name of the k8s API server ("https://hostname:port").

        Typically this is unnecessary, the hostname is picked up by
        config.load_incluster_config() or config.load_kube_config.
        """,
    )

    async def initialize_resources(self):
        """
        This contains logic that was formerly in `__init__`, but since
        it now involves async/await, it can't be there anymore.  It is
        intended that this method be called immediately after a new
        KubeIngressProxy object is created via `__init__`.
        """
        labels = {
            'component': self.component_label,
            'hub.jupyter.org/proxy-route': 'true',
        }
        await load_config(caller=self)
        self.core_api = shared_client('CoreV1Api')
        self.extension_api = shared_client('ExtensionsV1beta1Api')

        self.ingress_reflector = await IngressReflector.create(
            parent=self, namespace=self.namespace, labels=labels
        )
        self.service_reflector = await ServiceReflector.create(
            parent=self, namespace=self.namespace, labels=labels
        )
        self.endpoint_reflector = await EndpointsReflector.create(
            parent=self, namespace=self.namespace, labels=labels
        )

    def safe_name_for_routespec(self, routespec):
        safe_chars = set(string.ascii_lowercase + string.digits)
        safe_name = generate_hashed_slug(
            'jupyter-'
            + escapism.escape(routespec, safe=safe_chars, escape_char='-')
            + '-route'
        )
        return safe_name

    async def delete_if_exists(self, kind, safe_name, future):
        try:
            await future
            self.log.info('Deleted %s/%s', kind, safe_name)
        except client.rest.ApiException as e:
            if e.status != 404:
                raise
            self.log.warn("Could not delete %s/%s: does not exist", kind, safe_name)

    async def add_route(self, routespec, target, data):
        # Create a route with the name being escaped routespec
        # Use full routespec in label
        # 'data' is JSON encoded and put in an annotation - we don't need to query for it

        safe_name = self.safe_name_for_routespec(routespec).lower()
        labels = {
            'heritage': 'jupyterhub',
            'component': self.component_label,
            'hub.jupyter.org/proxy-route': 'true',
        }
        endpoint, service, ingress = make_ingress(
            safe_name, routespec, target, labels, data
        )

        async def ensure_object(create_func, patch_func, body, kind):
            try:
                resp = await create_func(namespace=self.namespace, body=body)
                self.log.info('Created %s/%s', kind, safe_name)
            except client.rest.ApiException as e:
                if e.status == 409:
                    # This object already exists, we should patch it to make it be what we want
                    self.log.warn(
                        "Trying to patch %s/%s, it already exists", kind, safe_name
                    )
                    resp = await patch_func(
                        namespace=self.namespace,
                        body=body,
                        name=body.metadata.name,
                    )
                else:
                    raise

        if endpoint is not None:
            await ensure_object(
                self.core_api.create_namespaced_endpoints,
                self.core_api.patch_namespaced_endpoints,
                body=endpoint,
                kind='endpoints',
            )

            await exponential_backoff(
                lambda: f'{self.namespace}/{safe_name}'
                in self.endpoint_reflector.endpoints.keys(),
                'Could not find endpoints/%s after creating it' % safe_name,
            )
        else:
            delete_endpoint = await self.core_api.delete_namespaced_endpoints(
                name=safe_name,
                namespace=self.namespace,
                body=client.V1DeleteOptions(grace_period_seconds=0),
            )
            await self.delete_if_exists('endpoint', safe_name, delete_endpoint)

        await ensure_object(
            self.core_api.create_namespaced_service,
            self.core_api.patch_namespaced_service,
            body=service,
            kind='service',
        )

        await exponential_backoff(
            lambda: f'{self.namespace}/{safe_name}'
            in self.service_reflector.services.keys(),
            'Could not find service/%s after creating it' % safe_name,
        )

        await ensure_object(
            self.extension_api.create_namespaced_ingress,
            self.extension_api.patch_namespaced_ingress,
            body=ingress,
            kind='ingress',
        )

        await exponential_backoff(
            lambda: f'{self.namespace}/{safe_name}'
            in self.ingress_reflector.ingresses.keys(),
            'Could not find ingress/%s after creating it' % safe_name,
        )

    async def delete_route(self, routespec):
        # We just ensure that these objects are deleted.
        # This means if some of them are already deleted, we just let it
        # be.

        safe_name = self.safe_name_for_routespec(routespec).lower()

        delete_options = client.V1DeleteOptions(grace_period_seconds=0)

        delete_endpoint = await self.core_api.delete_namespaced_endpoints(
            name=safe_name,
            namespace=self.namespace,
            body=delete_options,
        )

        delete_service = await self.core_api.delete_namespaced_service(
            name=safe_name,
            namespace=self.namespace,
            body=delete_options,
        )

        delete_ingress = await self.extension_api.delete_namespaced_ingress(
            name=safe_name,
            namespace=self.namespace,
            body=delete_options,
            grace_period_seconds=0,
        )

        # This seems like cleanest way to parallelize all three of these while
        # also making sure we only ignore the exception when it's a 404.
        # The order matters for endpoint & service - deleting the service deletes
        # the endpoint in the background. This can be racy however, so we do so
        # explicitly ourselves as well. In the future, we can probably try a
        # foreground cascading deletion (https://kubernetes.io/docs/concepts/workloads/controllers/garbage-collection/#foreground-cascading-deletion)
        # instead, but for now this works well enough.
        await asyncio.gather(
            self.delete_if_exists('endpoint', safe_name, delete_endpoint),
            self.delete_if_exists('service', safe_name, delete_service),
            self.delete_if_exists('ingress', safe_name, delete_ingress),
        )

    async def get_all_routes(self):
        # copy everything, because iterating over this directly is not threadsafe
        # FIXME: is this performance intensive? It could be! Measure?
        # FIXME: Validate that this shallow copy *is* thread safe
        ingress_copy = dict(self.ingress_reflector.ingresses)
        routes = {
            ingress["metadata"]["annotations"]['hub.jupyter.org/proxy-routespec']: {
                'routespec': ingress["metadata"]["annotations"][
                    'hub.jupyter.org/proxy-routespec'
                ],
                'target': ingress["metadata"]["annotations"][
                    'hub.jupyter.org/proxy-target'
                ],
                'data': json.loads(
                    ingress["metadata"]["annotations"]['hub.jupyter.org/proxy-data']
                ),
            }
            for ingress in ingress_copy.values()
        }

        return routes
