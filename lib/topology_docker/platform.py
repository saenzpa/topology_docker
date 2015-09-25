# -*- coding: utf-8 -*-
#
# Copyright (C) 2015 Hewlett Packard Enterprise Development LP <asicapi@hp.com>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""
Docker engine platform module for topology.
"""

from __future__ import unicode_literals, absolute_import
from __future__ import print_function, division

import logging
import time

from docker import Client

from subprocess import check_call
from shlex import split as shplit
from pexpect import spawn

from topology.platforms.base import BasePlatform, CommonNode

log = logging.getLogger(__name__)


class DockerPlatform(BasePlatform):
    """
    Plugin to build a topology using Docker.

    See :class:`topology.platforms.base.BasePlatform` for more information.
    """

    def __init__(self, timestamp, nmlmanager):
        self.nmlnode_node_map = {}

    def pre_build(self):
        """
        See :meth:`BasePlatform.pre_build` for more information.
        """

    def add_node(self, node):
        """
        Add new switch or host node.

        See :meth:`BasePlatform.add_node` for more information.
        """
        node_type = node.metadata.get('type', 'switch')
        image = node.metadata.get('image', 'ubuntu')

        enode = None

        if node_type == 'switch':
            enode = DockerSwitch(str(node.identifier), image=image)
        elif node_type == 'host':
            enode = DockerHost(str(node.identifier), image=image)
        else:
            raise Exception('Unsupported type {}'.format(node_type))

        # FIXME: consider moving start to post_build
        enode.start()

        self.nmlnode_node_map[node.identifier] = enode
        return enode

    def add_biport(self, node, biport):
        """
        Add a port to the docker node.

        See :meth:`BasePlatform.add_biport` for more information.
        """
        enode = self.nmlnode_node_map[node.identifier]
        enode.add_port(biport)

    def add_bilink(self, nodeport_a, nodeport_b, bilink):
        """
        Add a link between two nodes.

        See :meth:`BasePlatform.add_bilink` for more information.
        """
        enode_a = self.nmlnode_node_map[
            nodeport_a[0].identifier]
        netns_a = enode_a.name
        enode_b = self.nmlnode_node_map[
            nodeport_b[0].identifier]
        netns_b = enode_b.name

        intf_a = nodeport_a[1].identifier
        intf_b = nodeport_b[1].identifier

        # Check docs.docker.com/articles/networking/#building-a-point-to-point-connection # noqa
        command_template = """ \
            ip link add {intf_a} type veth peer name {intf_b}
            ip link set {intf_a} netns {netns_a}
            ip netns exec {netns_a} ip link set dev {intf_a} up
            ip link set {intf_b} netns {netns_b}
            ip netns exec {netns_b} ip link set dev {intf_b} up\
            """
        commands = command_template.format(**locals())

        for command in commands.splitlines():
            check_call(shplit(command.lstrip()))

        enode_a.add_link(nodeport_a[1])
        enode_b.add_link(nodeport_b[1])

    def post_build(self):
        """
        Ports are created for each node automatically while adding links.
        Creates the rest of the ports (no-linked ports)

        See :meth:`BasePlatform.post_build` for more information.
        """
        for enode in self.nmlnode_node_map.values():
            enode.create_unlinked_ports()

    def destroy(self):
        """
        See :meth:`BasePlatform.destroy` for more information.
        """
        for enode in self.nmlnode_node_map.values():
            enode.stop()


class DockerNode(CommonNode):

    """
    An instance of this class will create a detached Docker container.

    :param str name: The name of the node.
    :param str image: The image to run on this node.
    :param str command: The command to run when the container is brought up.
    """

    def __init__(self, name, image='ubuntu', command='bash', **kwargs):
        if name is None:
            name = str(id(self))

        self.name = name
        self._image = image
        self._command = command
        self._client = Client()
        self._container_id = self._client.create_container(
            image=self._image,
            command=self._command,
            name=name,
            detach=True,
            tty=True,
            host_config=self._client.create_host_config(
                privileged=True,     # Container is given access to all devices
                network_mode='none'  # Avoid connecting to host bridge,
                                     # usually docker0

            )
        )['Id']

        self._bash = spawn(
            'docker exec -i -t {} bash'.format(name)
        )

        self._port_status = {}

        super(DockerNode, self).__init__(name, **kwargs)
        self._shells['bash'] = self.bash

    def add_port(self, port):
        """
        Add port to node list, doesn't actually add port to docker.
        """
        self._port_status[port.identifier] = 'down'

    def add_link(self, port):
        """
        Marks port as linked, meaning that the port was created on docker
        by a link.
        """
        self._port_status[port.identifier] = 'linked'

    def create_unlinked_ports(self):
        """
        Iterates the node port list and create a tuntap interface for each
        port that was added but not linked.
        """
        # FIXME: use send_command.
        command_template = \
            'docker exec {name} ip tuntap add dev {port} mode tap'
        for port, status in self._port_status.items():
            if status == 'down':
                check_call(shplit(
                    command_template.format(name=self.name, port=port)))
                self._port_status[port] = 'up'

    def _create_netns(self):
        """
        Docker creates a netns. This method makes that netns avaible
        to the host
        """
        pid = self._client.inspect_container(
            self._container_id)['State']['Pid']
        name = self.name

        command_template = """ \
            mkdir -p /var/run/netns
            ln -s /proc/{pid}/ns/net /var/run/netns/{name} \
            """
        commands = command_template.format(**locals())

        for command in commands.splitlines():
            check_call(shplit(command.lstrip()))

    def bash(self, command):
        self._bash.sendline(command)
        time.sleep(0.2)
        # Without this sleep, the content of self._bash.after is truncated
        # most of the time this method is called.
        # I tried first with a 0.1 value and self._bash.after was truncated
        # 2 out of 100 times. When I tried with 0.2, it was not truncated
        # after 100 runs.
        self._bash.expect('.*#')  # FIXME: Add a proper regex.
        return self._bash.after

    def start(self):
        """
        Start the docker node and configures a netns for it.
        """
        self._client.start(self._container_id)
        self._create_netns()

    def stop(self):
        """
        Stop docker container and remove its netns
        """
        self._client.stop(self._container_id)
        self._client.wait(self._container_id)
        self._client.remove_container(self._container_id)

        # remove netns
        command_template = "ip netns del {self.name}"
        command = command_template.format(**locals())
        check_call(shplit(command))


class DockerSwitch(DockerNode):
    def __init__(self, name, image='ubuntu', command='bash', **kwargs):
        super(DockerSwitch, self).__init__(name, image, command, **kwargs)
        self._vtysh = spawn(
            'docker exec -i -t {} vtysh'.format(name)
        )
        self._shells['vtysh'] = self.vtysh

    def vtysh(self, command):
        self._vtysh.sendline(command)
        time.sleep(0.2)  # FIXME: Find out minimal value that passes 100 tests.
        self._vtysh.expect('.*#')  # FIXME: Add a proper regex.
        return self._vtysh.after


class DockerHost(DockerNode):
    pass


__all__ = ['DockerHost', 'DockerSwitch']