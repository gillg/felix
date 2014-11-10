# -*- coding: utf-8 -*-
# Copyright 2014 Metaswitch Networks
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
felix.futils
~~~~~~~~~~~~

Felix utilities.
"""
import iptc
import logging
import os
import re
import subprocess
import time

from calico.felix.config import Config

# Logger
log = logging.getLogger(__name__)

# Special value to mean "put this rule at the end".
RULE_POSN_LAST = -1

# Chain names
CHAIN_PREROUTING         = "felix-PREROUTING"
CHAIN_INPUT              = "felix-INPUT"
CHAIN_FORWARD            = "felix-FORWARD"
CHAIN_TO_PREFIX          = "felix-to-"
CHAIN_FROM_PREFIX        = "felix-from-"

#*****************************************************************************#
#* ipset names. An ipset can either have a port and protocol or not - it     *#
#* cannot have a mix of members with and without them. The "to" ipsets are   *#
#* referenced from the "to" chains, and the "from" ipsets from the "from"    *#
#* chains.                                                                   *#
#*****************************************************************************#
IPSET_TO_ADDR_PREFIX    = "felix-to-addr-"
IPSET_TO_PORT_PREFIX    = "felix-to-port-"
IPSET_FROM_ADDR_PREFIX  = "felix-from-addr-"
IPSET_FROM_PORT_PREFIX  = "felix-from-port-"
IPSET6_TO_ADDR_PREFIX   = "felix-6-to-addr-"
IPSET6_TO_PORT_PREFIX   = "felix-6-to-port-"
IPSET6_FROM_ADDR_PREFIX = "felix-6-from-addr-"
IPSET6_FROM_PORT_PREFIX = "felix-6-from-port-"
IPSET_TMP_PORT          = "felix-tmp-port"
IPSET_TMP_ADDR          = "felix-tmp-addr"
IPSET6_TMP_PORT         = "felix-6-tmp-port"
IPSET6_TMP_ADDR         = "felix-6-tmp-addr"

# Flag to indicate "IP v4" or "IP v6"; format that can be printed in logs.
IPV4 = "IPv4"
IPV6 = "IPv6"

# Regexes for IP addresses.
IPV4_REGEX = re.compile("\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}")
IPV6_REGEX = re.compile("[a-f0-9]+:[:a-f0-9]+")

#*****************************************************************************#
#* Load the conntrack tables. This is a workaround for this issue            *#
#* https://github.com/ldx/python-iptables/issues/112                         *#
#*                                                                           *#
#* It forces all extensions to be loaded at start of day then stored so they *#
#* cannot be unloaded (and hence reloaded).                                  *#
#*****************************************************************************#
global_rule  = iptc.Rule()
global_rule6 = iptc.Rule6()
global_rule.create_match("conntrack")
global_rule6.create_match("conntrack")
global_rule.create_match("tcp")
global_rule6.create_match("tcp")
global_rule6.create_match("icmp6")
global_rule.create_match("udp")
global_rule6.create_match("udp")
global_rule.create_match("mac")
global_rule6.create_match("mac")
global_rule.create_match("physdev")
global_rule6.create_match("physdev")

# Attach some targets.
global_rule.create_target("RETURN")
global_rule6.create_target("RETURN")
global_target = iptc.Target(global_rule, "DNAT")


def tap_exists(tap):
    """
    Returns True if tap device exists.
    """
    return os.path.exists("/sys/class/net/" + tap)


def list_tap_ips(type, tap):
    """
    List IP addresses for which there are routes to a given tap interface.
    Returns a set with all addresses for which there is a route to the device.
    """
    ips = set()

    if type == IPV4:
        data = subprocess.check_output(
            ["ip", "route", "list", "dev", tap])
    else:
        data = subprocess.check_output(
            ["ip", "-6", "route", "list", "dev", tap])

    lines = data.split("\n")

    log.debug("Existing routes to %s : %s" % (tap, ",".join(lines)))

    for line in lines:
        #*********************************************************************#
        #* Example of the lines we care about is (having specified the       *#
        #* device above) :                                                   *#
        #* 10.11.2.66 proto static scope link                                *#
        #*********************************************************************#
        words = line.split()

        if len(words) > 1:
            ip = words[0]
            if IPV4_REGEX.match(ip) or IPV6_REGEX.match(ip):
                # Looks like an IP address to me
                ips.add(words[0])
            else:
                # Not an IP address; seems odd.
                log.warning("No IP address found in line %s for %s" %
                            (line, tap))

    return ips


def add_route(type, ip, tap, mac):
    """
    Add a route to a given tap interface (including arp config).
    Errors lead to exceptions that are not handled here.
    """
    if type == IPV4:
        subprocess.check_call(['arp', '-s', ip, mac, '-i', tap])
        subprocess.check_call(["ip", "route", "add", ip, "dev", tap])
    else:
        subprocess.check_call(["ip", "-6", "route", "add", ip, "dev", tap])


def del_route(type, ip, tap):
    """
    Delete a route to a given tap interface (including arp config).
    Errors lead to exceptions that are not handled here.
    """
    if type == IPV4:
        subprocess.check_call(['arp', '-d', ip, '-i', tap])
        subprocess.check_call(["ip", "route", "del", ip, "dev", tap])
    else:
        subprocess.check_call(["ip", "-6", "route", "del", ip, "dev", tap])


def configure_tap(tap):
    """
    Configure the various proc file system parameters for the tap interface.

    Specifically, allow packets from tap interfaces to be directed to
    localhost, and enable proxy ARP.
    """
    with open('/proc/sys/net/ipv4/conf/%s/route_localnet' % tap, 'wb') as f:
        f.write('1')

    with open("/proc/sys/net/ipv4/conf/%s/proxy_arp" % tap, 'wb') as f:
        f.write('1')


def insert_rule(rule, chain, position=0):
    """
    Add an iptables rule to a chain if it does not already exist. Position
    is the position for the insert as an offset; if set to
    futils.RULE_POSN_LAST then the rule is appended.
    """
    found = False
    rules = chain.rules

    if position == RULE_POSN_LAST:
        position = len(rules)

    # The python-iptables code to compare rules does a comparison on all the
    # relevant rule parameters (target, match, etc.) excluding the offset into
    # the chain. Hence the test below finds whether there is a rule with the
    # same parameters anywhere in the chain.
    if rule not in chain.rules:
        chain.insert_rule(rule, position)


def get_rule(type):
    """
    Gets a new empty rule. This is a simple helper method that returns either
    an IP v4 or an IP v6 rule according to type.
    """
    if type == IPV4:
        rule = iptc.Rule()
    else:
        rule = iptc.Rule6()
    return rule


def get_table(type, name):
    """
    Gets a table. This is a simple helper method that returns either
    an IP v4 or an IP v6 table according to type.
    """
    if type == IPV4:
        table = iptc.Table(name)
    else:
        table = iptc.Table6(name)

    return table


def set_global_rules():
    """
    Set up global iptables rules. These are rules that do not change with
    endpoint, and are expected never to change - but they must be present.
    """
    # The nat tables first. This must have a felix-PREROUTING chain.
    table = iptc.Table(iptc.Table.NAT)
    chain = create_chain(table, CHAIN_PREROUTING)

    # Now add the single rule to that chain. It looks like this.
    #  DNAT tcp -- any any anywhere 169.254.169.254 tcp dpt:http to:127.0.0.1:9697
    rule          = iptc.Rule()
    rule.dst      = "169.254.169.254"
    rule.protocol = "tcp"
    target        = iptc.Target(rule, "DNAT")
    target.to_destination = "127.0.0.1:9697"
    rule.target = target
    match = iptc.Match(rule, "tcp")
    match.dport = "80"
    rule.add_match(match)
    insert_rule(rule, chain)

    #*************************************************************************#
    #* This is a hack, because of a bug in python-iptables where it fails to *#
    #* correctly match some rules; see                                       *#
    #* https://github.com/ldx/python-iptables/issues/111 If any of the rules *#
    #* relating to this tap device already exist, assume that they all do so *#
    #* as not to recreate them.                                              *#
    #*                                                                       *#
    #* This is Calico issue #35,                                             *#
    #* https://github.com/Metaswitch/calico/issues/35                        *#
    #*************************************************************************#
    rules_check = subprocess.call("iptables -L %s | grep %s" %
                                  ("INPUT", CHAIN_INPUT),
                                  shell=True)

    if rules_check == 0:
        log.debug("Static rules already exist")
    else:
        # Add a rule that forces us through the chain we just created.
        chain = iptc.Chain(table, "PREROUTING")
        rule  = iptc.Rule()
        rule.create_target(CHAIN_PREROUTING)
        insert_rule(rule, chain)

    #*************************************************************************#
    #* Now the filter table. This needs to have calico-filter-FORWARD and    *#
    #* calico-filter-INPUT chains, which we must create before adding any    *#
    #* rules that send to them.                                              *#
    #*************************************************************************#
    for type in (IPV4, IPV6):
        table = get_table(type, iptc.Table.FILTER)
        create_chain(table, CHAIN_FORWARD)
        create_chain(table, CHAIN_INPUT)

        if rules_check != 0:
            # Add rules that forces us through the chain we just created.
            chain = iptc.Chain(table, "FORWARD")
            rule  = get_rule(type)
            rule.create_target(CHAIN_FORWARD)
            insert_rule(rule, chain)

            chain = iptc.Chain(table, "INPUT")
            rule  = get_rule(type)
            rule.create_target(CHAIN_INPUT)
            insert_rule(rule, chain)


def set_ep_specific_rules(id, iface, type, localips, mac):
    """
    Add (or modify) the rules for a particular endpoint, whose id is
    supplied. This routine :
    - ensures that the chains specific to this endpoint exist, where there is
      a chain for packets leaving and a chain for packets arriving at the
      endpoint;
    - routes packets to / from the tap interface to the chains created above;
    - fills out the endpoint specific chains with the correct rules;
    - verifies that the ipsets exist.

    The net of all this is that every bit of iptables configuration that is
    specific to this particular endpoint is created (or verified), with the
    exception of ACLs (i.e. the configuration of the list of other addresses
    for which routing is permitted) - this is done in set_acls.
    Note however that this routine handles IPv4 or IPv6 not both; it is
    normally called twice in succession (once for each).
    """
    to_chain_name   = CHAIN_TO_PREFIX + id
    from_chain_name = CHAIN_FROM_PREFIX + id

    # Set up all the ipsets.
    if type == IPV4:
        to_ipset_port     = IPSET_TO_PORT_PREFIX + id
        to_ipset_addr   = IPSET_TO_ADDR_PREFIX + id
        from_ipset_port   = IPSET_FROM_PORT_PREFIX + id
        from_ipset_addr = IPSET_FROM_ADDR_PREFIX + id
        family            = "inet"
    else:
        to_ipset_port     = IPSET6_TO_PORT_PREFIX + id
        to_ipset_addr   = IPSET6_TO_ADDR_PREFIX + id
        from_ipset_port   = IPSET6_FROM_PORT_PREFIX + id
        from_ipset_addr = IPSET6_FROM_ADDR_PREFIX + id
        family            = "inet6"

    # Create ipsets if they do not already exist.
    create_ipset(to_ipset_port, "hash:net,port", family)
    create_ipset(to_ipset_addr, "hash:net", family)
    create_ipset(from_ipset_port, "hash:net,port", family)
    create_ipset(from_ipset_addr, "hash:net", family)

    # Get the table.
    if type == IPV4:
        table  = iptc.Table(iptc.Table.FILTER)
    else:
        table  = iptc.Table6(iptc.Table6.FILTER)

    # Create the chains for packets to the interface
    to_chain = create_chain(table, to_chain_name)

    #*************************************************************************#
    #* Put rules into that chain. Note that we never ACCEPT, but always      *#
    #* RETURN if we want to accept this packet. This is because the rules    *#
    #* here are for this endpoint only - we cannot (for example) ACCEPT a    *#
    #* packet which would be rejected by the rules for another endpoint on   *#
    #* the same host to which it is addressed.                               *#
    #*************************************************************************#
    index = 0

    if type == IPV6:
        #************************************************************************#
        #* In ipv6 only, there are 6 rules that need to be created first.       *#
        #* RETURN ipv6-icmp anywhere anywhere ipv6-icmptype 130                 *#
        #* RETURN ipv6-icmp anywhere anywhere ipv6-icmptype 131                 *#
        #* RETURN ipv6-icmp anywhere anywhere ipv6-icmptype 132                 *#
        #* RETURN ipv6-icmp anywhere anywhere ipv6-icmp router-advertisement    *#
        #* RETURN ipv6-icmp anywhere anywhere ipv6-icmp neighbour-solicitation  *#
        #* RETURN ipv6-icmp anywhere anywhere ipv6-icmp neighbour-advertisement *#
        #*                                                                      *#
        #* These rules are ICMP types 130, 131, 132, 134, 135 and 136, and can  *#
        #* be created on the command line with something like :                 *#
        #*    ip6tables -A plw -j RETURN --protocol icmpv6 --icmpv6-type 130    *#
        #************************************************************************#
        for icmp in ["130", "131", "132", "134", "135", "136"]:
            rule          = iptc.Rule6()
            rule.create_target("RETURN")
            rule.protocol = "icmpv6"
            match = iptc.Match(rule, "icmp6")
            match.icmpv6_type = [icmp]
            rule.add_match(match)
            insert_rule(rule, to_chain, index)
            index += 1

    rule = get_rule(type)
    rule.create_target("DROP")
    match = rule.create_match("conntrack")
    match.ctstate = ["INVALID"]
    insert_rule(rule, to_chain, index)
    index += 1

    # "Return if state RELATED or ESTABLISHED".
    rule = get_rule(type)
    rule.create_target("RETURN")
    match = rule.create_match("conntrack")
    match.ctstate = ["RELATED,ESTABLISHED"]
    insert_rule(rule, to_chain, index)
    index += 1

    # "Return anything whose sources matches this ipset" (for two ipsets)
    rule = get_rule(type)
    rule.create_target("RETURN")
    match = iptc.Match(rule, "set")
    match.match_set = [to_ipset_port, "src"]
    rule.add_match(match)
    insert_rule(rule, to_chain, index)
    index += 1

    rule = get_rule(type)
    rule.create_target("RETURN")
    match = iptc.Match(rule, "set")
    match.match_set = [to_ipset_addr, "src"]
    rule.add_match(match)
    insert_rule(rule, to_chain, index)
    index += 1

    # Finally, "DROP unconditionally"
    rule = get_rule(type)
    rule.create_target("DROP")
    insert_rule(rule, to_chain, index)

    #*************************************************************************#
    #* Now the chain that manages packets from the interface, and the rules  *#
    #* in that chain.                                                        *#
    #*************************************************************************#
    from_chain = create_chain(table, from_chain_name)

    index = 0
    if type == IPV6:
        # In ipv6 only, allows all ICMP traffic from this endpoint to anywhere.
        rule = iptc.Rule6()
        rule.create_target("RETURN")
        rule.protocol = "icmpv6"
        insert_rule(rule, from_chain, index)
        index += 1

    # "Drop if state INVALID".
    rule = get_rule(type)
    rule.create_target("DROP")
    match = rule.create_match("conntrack")
    match.ctstate = ["INVALID"]
    insert_rule(rule, from_chain, index)
    index += 1

    # "Return if state RELATED or ESTABLISHED".
    rule = get_rule(type)
    rule.create_target("RETURN")
    match = rule.create_match("conntrack")
    match.ctstate = ["RELATED,ESTABLISHED"]
    insert_rule(rule, from_chain, index)
    index += 1

    if type == IPV4:
        #*********************************************************************#
        #* Drop UDP that would allow this server to act as a DHCP server.    *#
        #* This may be unnecessary - see                                     *#
        #* https://github.com/Metaswitch/calico/issues/36                    *#
        #*********************************************************************#
        rule          = iptc.Rule()
        rule.protocol = "udp"
        rule.create_target("DROP")
        match = iptc.Match(rule, "udp")
        match.sport = "67"
        match.dport = "68"
        rule.add_match(match)
        insert_rule(rule, from_chain, index)
        index += 1

    # "Drop packets whose destination matches the supplied ipset."
    rule = get_rule(type)
    rule.create_target("DROP")
    match = iptc.Match(rule, "set")
    match.match_set = [from_ipset_port, "dst"]
    rule.add_match(match)
    insert_rule(rule, from_chain, index)
    index += 1

    rule = get_rule(type)
    rule.create_target("DROP")
    match = iptc.Match(rule, "set")
    match.match_set = [from_ipset_addr, "dst"]
    rule.add_match(match)
    insert_rule(rule, from_chain, index)
    index += 1

    #*************************************************************************#
    #* Now allow through packets from the correct MAC and IP address. There  *#
    #* may be rules here from addresses that this endpoint no longer has -   *#
    #* in which case we must remove them.                                    *#
    #*                                                                       *#
    #* This code is rather ugly - better to turn off table autocommit, but   *#
    #* as commented elsewhere, that appears buggy.                           *#
    #*************************************************************************#
    done = False
    while not done:
        done = True
        for rule in from_chain.rules:
            if (rule.target.name == "RETURN" and
                    len(rule.matches) == 1 and
                    rule.matches[0].name == "mac" and
                    (rule.src not in localips or rule.match.mac_source != mac)):
                #*************************************************************#
                #* We have a rule that we should not have; either the MAC or *#
                #* the IP has changed. Toss the rule.                        *#
                #*************************************************************#
                log.info("Removing old IP %s, MAC %s from endpoint %s" %
                         (rule.src, rule.matches[0].mac_source, id))
                from_chain.delete_rule(rule)
                done = False
                break

    for ip in localips:
        rule = get_rule(type)
        rule.create_target("RETURN")
        rule.src         = ip
        match            = iptc.Match(rule, "mac")
        match.mac_source = mac
        rule.add_match(match)
        insert_rule(rule, from_chain, index)
        index += 1

    # Last rule (at end) says drop unconditionally.
    rule = get_rule(type)
    rule.create_target("DROP")
    insert_rule(rule, from_chain, RULE_POSN_LAST)

    #*************************************************************************#
    #* This is a hack, because of a bug in python-iptables where it fails to *#
    #* correctly match some rules; see                                       *#
    #* https://github.com/ldx/python-iptables/issues/111 If any of the rules *#
    #* relating to this tap device already exist, assume that they all do so *#
    #* as not to recreate them.                                              *#
    #*                                                                       *#
    #* This is Calico issue #35,                                             *#
    #* https://github.com/Metaswitch/calico/issues/35                        *#
    #*************************************************************************#
    if type == IPV4:
        rules_check = subprocess.call("iptables -L %s | grep %s > /dev/null" %
                                      (CHAIN_INPUT, iface),
                                      shell=True)
    else:
        rules_check = subprocess.call("ip6tables -L %s | grep %s > /dev/null" %
                                      (CHAIN_INPUT, iface),
                                      shell=True)

    if rules_check == 0:
        log.debug("%s rules for interface %s already exist" % (type, iface))
    else:
        #*********************************************************************#
        #* We have created the chains and rules that control input and       *#
        #* output for the interface but not routed traffic through them. Add *#
        #* the input rule detecting packets arriving for the endpoint.  Note *#
        #* that these rules should perhaps be restructured and simplified    *#
        #* given that this is not a bridged network -                        *#
        #* https://github.com/Metaswitch/calico/issues/36                    *#
        #*********************************************************************#
        log.debug("%s rules for interface %s do not already exist" %
                  (type, iface))
        chain = create_chain(table, CHAIN_INPUT)
        rule  = get_rule(type)
        target        = iptc.Target(rule, from_chain_name)
        rule.target   = target
        match = iptc.Match(rule, "physdev")
        match.physdev_in = iface
        match.physdev_is_bridged = ""
        rule.add_match(match)
        insert_rule(rule, chain, RULE_POSN_LAST)

        #*********************************************************************#
        #* Similarly, create the rules that direct packets that are          *#
        #* forwarded either to or from the endpoint, sending them to the     *#
        #* "to" or "from" chains as appropriate.                             *#
        #*********************************************************************#
        chain = create_chain(table, CHAIN_FORWARD)
        rule  = get_rule(type)
        target        = iptc.Target(rule, from_chain_name)
        rule.target   = target
        match = iptc.Match(rule, "physdev")
        match.physdev_in = iface
        match.physdev_is_bridged = ""
        rule.add_match(match)
        insert_rule(rule, chain, RULE_POSN_LAST)

        rule          = get_rule(type)
        target        = iptc.Target(rule, to_chain_name)
        rule.target   = target
        match = iptc.Match(rule, "physdev")
        match.physdev_out = iface
        match.physdev_is_bridged = ""
        rule.add_match(match)
        insert_rule(rule, chain, RULE_POSN_LAST)

        rule               = get_rule(type)
        target             = iptc.Target(rule, to_chain_name)
        rule.target        = target
        rule.out_interface = iface
        insert_rule(rule, chain, RULE_POSN_LAST)


def del_rules(id, type):
    """
    Remove the rules for an endpoint which is no longer managed.
    """
    log.debug("Delete %s rules for %s" % (type, id))
    to_chain   = CHAIN_TO_PREFIX + id
    from_chain = CHAIN_FROM_PREFIX + id

    if type == IPV4:
        to_ipset_port   = IPSET_TO_PORT_PREFIX + id
        to_ipset_addr   = IPSET_TO_ADDR_PREFIX + id
        from_ipset_port = IPSET_FROM_PORT_PREFIX + id
        from_ipset_addr = IPSET_FROM_ADDR_PREFIX + id

        table = iptc.Table(iptc.Table.FILTER)
    else:
        to_ipset_port   = IPSET6_TO_PORT_PREFIX + id
        to_ipset_addr   = IPSET6_TO_ADDR_PREFIX + id
        from_ipset_port = IPSET6_FROM_PORT_PREFIX + id
        from_ipset_addr = IPSET6_FROM_ADDR_PREFIX + id

        table = iptc.Table6(iptc.Table6.FILTER)

    #*************************************************************************#
    #* Remove the rules routing to the chain we are about to remove The      *#
    #* baroque structure is caused by the python-iptables interface.         *#
    #* chain.rules returns a list of rules, each of which contains its index *#
    #* (i.e. position). If we get rules 7 and 8 and try to remove them in    *#
    #* that order, then the second fails because rule 8 got renumbered when  *#
    #* rule 7 was deleted, so the rule we have in our hand neither matches   *#
    #* the old rule 8 (now at index 7) or the new rule 8 (with a different   *#
    #* target etc. Hence each time we remove a rule we rebuild the list of   *#
    #* rules to iterate through.                                             *#
    #*                                                                       *#
    #* In principle we could use autocommit to make this much nicer (as the  *#
    #* python-iptables docs suggest), but in practice it seems a bit buggy,  *#
    #* and leads to errors elsewhere. Reversing the list sounds like it      *#
    #* should work too, but in practice does not.                            *#
    #*************************************************************************#
    for name in (CHAIN_INPUT, CHAIN_FORWARD):
        chain = create_chain(table, name)
        done  = False
        while not done:
            done = True
            for rule in chain.rules:
                if rule.target.name in (to_chain, from_chain):
                    chain.delete_rule(rule)
                    done = False
                    break

    # Delete the from and to chains for this endpoint.
    for name in (from_chain, to_chain):
        if table.is_chain(name):
            chain = create_chain(table, name)
            log.debug("Flush chain %s", name)
            chain.flush()
            log.debug("Delete chain %s", name)
            table.delete_chain(name)

    # Delete the ipsets for this endpoint.
    for ipset in [from_ipset_addr, from_ipset_port,
                  to_ipset_addr, to_ipset_port]:
        if call_silent(["ipset", "list", ipset]) == 0:
            subprocess.check_call(["ipset", "destroy", ipset])


def set_acls(id, type, inbound, in_default, outbound, out_default):
    """
    Set up the ACLs, making sure that they match.
    """
    if type == IPV4:
        to_ipset_port   = IPSET_TO_PORT_PREFIX + id
        to_ipset_addr   = IPSET_TO_ADDR_PREFIX + id
        from_ipset_port = IPSET_FROM_PORT_PREFIX + id
        from_ipset_addr = IPSET_FROM_ADDR_PREFIX + id
        tmp_ipset_port  = IPSET_TMP_PORT
        tmp_ipset_addr  = IPSET_TMP_ADDR
        family          = "inet"
    else:
        to_ipset_port   = IPSET6_TO_PORT_PREFIX + id
        to_ipset_addr   = IPSET6_TO_ADDR_PREFIX + id
        from_ipset_port = IPSET6_FROM_PORT_PREFIX + id
        from_ipset_addr = IPSET6_FROM_ADDR_PREFIX + id
        tmp_ipset_port  = IPSET6_TMP_PORT
        tmp_ipset_addr  = IPSET6_TMP_ADDR
        family          = "inet6"

    # Verify that the tmp ipsets exist and are empty.
    create_ipset(tmp_ipset_port, "hash:net,port", family)
    create_ipset(tmp_ipset_addr, "hash:net", family)

    subprocess.check_call(["ipset", "flush", tmp_ipset_port])
    subprocess.check_call(["ipset", "flush", tmp_ipset_addr])

    update_ipsets(inbound, "inbound " + type,
                  to_ipset_addr, to_ipset_port,
                  tmp_ipset_addr, tmp_ipset_port)
    update_ipsets(outbound, "outbound " + type,
                  from_ipset_addr, from_ipset_port,
                  tmp_ipset_addr, tmp_ipset_port)


def update_ipsets(rule_list,
                  description,
                  ipset_addr,
                  ipset_port,
                  tmp_ipset_addr,
                  tmp_ipset_port):
    for rule in rule_list:
        if rule['cidr'] is None:
            # No cidr - give up.
            log.error("Invalid %s rule without cidr for %s : %s",
                      (descr, id, rule))
            continue
        if rule['protocol'] is None and rule['port'] is not None:
            # No protocol - must also be no port.
            log.error("Invalid %s rule with port but no protocol for %s : %s",
                      (descr, id, rule))
            continue

        #*********************************************************************#
        #* The ipset format is something like "10.11.1.3,udp:0"              *#
        #* Further valid examples include                                    *#
        #*   10.11.1.0/24                                                    *#
        #*   10.11.1.0/24,tcp                                                *#
        #*   10.11.1.0/24,80                                                 *#
        #*********************************************************************#
        if rule['port'] is not None:
            value = "%s,%s:%s" % (rule['cidr'], rule['protocol'], rule['port'])
            subprocess.check_call(
                ["ipset", "add", tmp_ipset_port, value, "-exist"])
        elif rule['protocol'] is not None:
            value = "%s,%s:0" % (rule['cidr'], rule['protocol'])
            subprocess.check_call(
                ["ipset", "add", tmp_ipset_port, value, "-exist"])
        else:
            value = rule['cidr']
            subprocess.check_call(
                ["ipset", "add", tmp_ipset_addr, value, "-exist"])

    # Now that we have filled the tmp ipset, swap it with the real one.
    subprocess.check_call(["ipset", "swap", tmp_ipset_addr, ipset_addr])
    subprocess.check_call(["ipset", "swap", tmp_ipset_port, ipset_port])

    # Get the temporary ipsets clean again - we leave them existing but empty.
    subprocess.check_call(["ipset", "flush", tmp_ipset_port])
    subprocess.check_call(["ipset", "flush", tmp_ipset_addr])


def list_eps_with_rules(type):
    """
    Lists all of the endpoints for which rules exist and are owned by Felix.
    Returns a set of suffices, i.e. the start of the uuid / end of the
    interface name.

    The purpose of this routine is to get a list of endpoints (actually tap
    suffices) for which there is configuration that Felix might need to tidy up
    from a previous iteration.
    """

    #*************************************************************************#
    #* For chains, we check against the "to" chain, while for ipsets we      *#
    #* check against the "to-port" ipset. This isn't random; we absolutely   *#
    #* must check the first one created in the creation code above (and the  *#
    #* last one deleted), to catch the case where (for example) endpoint     *#
    #* creation created one ipset then Felix terminated, where we have to    *#
    #* detect that there is an ipset lying around that needs tidying up.     *#
    #*************************************************************************#
    if type == IPV4:
        table = iptc.Table(iptc.Table.FILTER)
    else:
        table = iptc.Table6(iptc.Table6.FILTER)

    eps  = {chain.name.replace(CHAIN_TO_PREFIX, "")
            for chain in table.chains
            if chain.name.startswith(CHAIN_TO_PREFIX)}

    data  = subprocess.check_output(["ipset", "list"])
    lines = data.split("\n")

    for line in lines:
        words = line.split()
        if (len(words) > 1 and words[0] == "Name:" and
                words[1].startswith(IPSET_TO_PORT_PREFIX)):
            eps.add(words[1].replace(IPSET_TO_PORT_PREFIX, ""))
        elif (len(words) > 1 and words[0] == "Name:" and
              words[1].startswith(IPSET6_TO_PORT_PREFIX)):
            eps.add(words[1].replace(IPSET6_TO_PORT_PREFIX, ""))
    return eps


def call_silent(args):
    """
    Wrapper round subprocess_call that discards all of the output to both
    stdout and stderr. *args* must be a list.
    """
    retcode = subprocess.call(args,
                              stdout=open('/dev/null', 'w'),
                              stderr=subprocess.STDOUT)

    return retcode


def create_ipset(name, typename, family):
    """
    Create an ipset. If it already exists, do nothing.

    *name* is the name of the ipset.
    *typename* must be a valid type, such as "hash:net" or "hash:net,port"
    *family* must be *inet* or *inet6*
    """
    if call_silent(["ipset", "list", name]) != 0:
        # ipset list failed - either does not exist, or an error. Either way,
        # try creation, throwing an error if it does not work.
        subprocess.check_call(
            ["ipset", "create", name, typename, "family", family],
            stdout=open('/dev/null', 'w'),
            stderr=subprocess.STDOUT)


def create_chain(table, name):
    if table.is_chain(name):
        chain = iptc.Chain(table, name)
    else:
        table.create_chain(name)
        chain = iptc.Chain(table, name)

    return chain
