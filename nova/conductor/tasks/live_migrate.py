# vim: tabstop=4 shiftwidth=4 softtabstop=4

#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from oslo.config import cfg

from nova.compute import flavors
from nova.compute import power_state
from nova.compute import rpcapi as compute_rpcapi
from nova.compute import utils as compute_utils
from nova import db
from nova import exception
from nova.image import glance
from nova.objects import base as obj_base
from nova.openstack.common.gettextutils import _
from nova.openstack.common import log as logging
from nova.scheduler import rpcapi as scheduler_rpcapi
from nova import servicegroup
from nova.scheduler.filters import utils
from nova.objects import aggregate



LOG = logging.getLogger(__name__)

migrate_opt = cfg.IntOpt('migrate_max_retries',
        default=-1,
        help='Number of times to retry live-migration before failing. '
             'If == -1, try until out of hosts. '
             'If == 0, only try once, no retries.')

CONF = cfg.CONF
CONF.register_opt(migrate_opt)


class LiveMigrationTask(object):
    def __init__(self, context, instance, destination,
                 block_migration, disk_over_commit):
        self.context = context
        self.instance = instance
        self.destination = destination
        self.block_migration = block_migration
        self.disk_over_commit = disk_over_commit
        self.source = instance.host
        self.migrate_data = None
        self.compute_rpcapi = compute_rpcapi.ComputeAPI()
        self.servicegroup_api = servicegroup.API()
        self.scheduler_rpcapi = scheduler_rpcapi.SchedulerAPI()
        self.image_service = glance.get_default_image_service()

    def execute(self):
        self._check_instance_is_running()
        self._check_host_is_up(self.source)

        if not self.destination:
            self.destination = self._find_destination()
        else:
            self._check_requested_destination()

        #TODO(johngarbutt) need to move complexity out of compute manager
        return self.compute_rpcapi.live_migration(self.context,
                host=self.source,
                instance=self.instance,
                dest=self.destination,
                block_migration=self.block_migration,
                migrate_data=self.migrate_data)
                #TODO(johngarbutt) disk_over_commit?

    def rollback(self):
        #TODO(johngarbutt) need to implement the clean up operation
        # but this will make sense only once we pull in the compute
        # calls, since this class currently makes no state changes,
        # except to call the compute method, that has no matching
        # rollback call right now.
        raise NotImplementedError()

    def _check_instance_is_running(self):
        if self.instance.power_state != power_state.RUNNING:
            raise exception.InstanceNotRunning(
                    instance_id=self.instance.uuid)

    def _check_host_is_up(self, host):
        try:
            service = db.service_get_by_compute_host(self.context, host)
        except exception.NotFound:
            raise exception.ComputeServiceUnavailable(host=host)

        if not self.servicegroup_api.service_is_up(service):
            raise exception.ComputeServiceUnavailable(host=host)

    def _check_requested_destination(self):
        self._check_destination_is_not_source()
        self._check_host_is_up(self.destination)
        self._check_destination_has_enough_memory()
        self._check_compatible_with_source_hypervisor(self.destination)
        self._call_livem_checks_on_host(self.destination)

    def _check_destination_is_not_source(self):
        if self.destination == self.source:
            raise exception.UnableToMigrateToSelf(
                    instance_id=self.instance.uuid, host=self.destination)

    def _get_aggregate_metadata(self,instance_uuid, dest):
        aggregate_list = db.aggregate_get_by_host(self.context, self.destination)
        LOG.debug(_("lwatta::====> aggregate_list size %s"), len(aggregate_list))
        if len(aggregate_list) == 1:
            return aggregate_list[0]

        # Check to see if host is in an aggregate or in more than one aggregate (cause we do stupid stuff)
        if len(aggregate_list) > 1:
            agg_name = []
            for i in aggregate_list:
                agg_name.append(i.name)
                LOG.debug(_("lwatta::====> agg_name %s"), agg_name)
                reason = _("Unable to migrate %(instance_uuid)s to %(dest)s: "
                "Destination host is in more then one aggregate:%(agg_name)s")
                raise exception.MigrationPreCheckError(reason=reason % dict(
                    instance_uuid=instance_uuid, dest=dest, agg_name=agg_name))
        elif len(aggregate_list) < 1:
            LOG.debug(_("lwatta::====> Host is not in an aggregate %s"), dest)
            reason = _("Unable to migrate %(instance_uuid)s to %(dest)s: "
            "Destination host is not in any aggregates")
            raise exception.MigrationPreCheckError(
                reason=reason % dict(instance_uuid=instance_uuid, dest=dest))


    def _check_destination_has_enough_memory(self):
        instance_uuid = self.instance.uuid
        dest = self.destination
        aggr = self._get_aggregate_metadata(instance_uuid, dest)
        # for icehouse replace aggr.metadetails with aggr.metadata.

        ram_ratio = float(aggr.get('metadetails')['ram_allocation_ratio'])
        LOG.debug(_("lwatta::====> Ram_allocation_ratio %s"), ram_ratio)

        total = self._get_compute_info(self.destination)['memory_mb']

        LOG.debug(_("lwatta::====> Total physical memory on host %s"), total)

        used = self._get_compute_info(self.destination)['memory_mb_used']

        mem_inst = self.instance.memory_mb
        LOG.debug(_("lwatta::====> instance total memory %s"), mem_inst)
        real_total = total * ram_ratio
        LOG.debug(_("lwatta::====> Oversubscribed total memory %s"), real_total)
        avail = real_total - used
        LOG.debug(_("lwatta::====> Ratio calculated avail is  %s"), avail)

        if not mem_inst or avail <= mem_inst:
            reason = _("Unable to migrate %(instance_uuid)s to %(dest)s: "
                       "Lack of memory(host:%(avail)s <= "
                       "instance:%(mem_inst)s)")
            raise exception.MigrationPreCheckError(reason=reason % dict(
                    instance_uuid=instance_uuid, dest=dest, avail=avail,
                    mem_inst=mem_inst))


    def _get_compute_info(self, host):
        service_ref = db.service_get_by_compute_host(self.context, host)
        return service_ref['compute_node'][0]

    def _check_compatible_with_source_hypervisor(self, destination):
        source_info = self._get_compute_info(self.source)
        destination_info = self._get_compute_info(destination)

        source_type = source_info['hypervisor_type']
        destination_type = destination_info['hypervisor_type']
        if source_type != destination_type:
            raise exception.InvalidHypervisorType()

        source_version = source_info['hypervisor_version']
        destination_version = destination_info['hypervisor_version']
        if source_version > destination_version:
            raise exception.DestinationHypervisorTooOld()

    def _call_livem_checks_on_host(self, destination):
        self.migrate_data = self.compute_rpcapi.\
            check_can_live_migrate_destination(self.context, self.instance,
                destination, self.block_migration, self.disk_over_commit)

    def _find_destination(self):
        #TODO(johngarbutt) this retry loop should be shared
        attempted_hosts = [self.source]
        image = None
        if self.instance.image_ref:
            image = compute_utils.get_image_metadata(self.context,
                                                     self.image_service,
                                                     self.instance.image_ref,
                                                     self.instance)
        instance_type = flavors.extract_flavor(self.instance)

        host = None
        while host is None:
            self._check_not_over_max_retries(attempted_hosts)

            host = self._get_candidate_destination(image,
                    instance_type, attempted_hosts)
            try:
                self._check_compatible_with_source_hypervisor(host)
                self._call_livem_checks_on_host(host)
            except exception.Invalid as e:
                LOG.debug(_("Skipping host: %(host)s because: %(e)s") %
                    {"host": host, "e": e})
                attempted_hosts.append(host)
                host = None
        return host

    def _get_candidate_destination(self, image, instance_type,
                                   attempted_hosts):
        instance_p = obj_base.obj_to_primitive(self.instance)
        request_spec = {'instance_properties': instance_p,
                        'instance_type': instance_type,
                        'instance_uuids': [self.instance.uuid]}
        if image:
            request_spec['image'] = image
        filter_properties = {'ignore_hosts': attempted_hosts}
        return self.scheduler_rpcapi.select_hosts(self.context,
                        request_spec, filter_properties)[0]

    def _check_not_over_max_retries(self, attempted_hosts):
        if CONF.migrate_max_retries == -1:
            return

        retries = len(attempted_hosts) - 1
        if retries > CONF.migrate_max_retries:
            msg = (_('Exceeded max scheduling retries %(max_retries)d for '
                     'instance %(instance_uuid)s during live migration')
                   % {'max_retries': retries,
                      'instance_uuid': self.instance.uuid})
            raise exception.NoValidHost(reason=msg)


def execute(context, instance, destination,
            block_migration, disk_over_commit):
    task = LiveMigrationTask(context, instance,
                             destination,
                             block_migration,
                             disk_over_commit)
    #TODO(johngarbutt) create a superclass that contains a safe_execute call
    return task.execute()
