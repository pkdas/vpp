#!/usr/bin/env python

import unittest
import socket

from framework import VppTestCase, VppTestRunner
from vpp_ip_route import VppIpRoute, VppRoutePath, VppMplsRoute, \
    VppMplsTable, VppIpMRoute, VppMRoutePath, VppIpTable, \
    MRouteEntryFlags, MRouteItfFlags, MPLS_LABEL_INVALID, DpoProto
from vpp_bier import *
from vpp_udp_encap import *

from scapy.packet import Raw
from scapy.layers.l2 import Ether
from scapy.layers.inet import IP, UDP, ICMP
from scapy.layers.inet6 import IPv6
from scapy.contrib.mpls import MPLS
from scapy.contrib.bier import *


class TestBFIB(VppTestCase):
    """ BIER FIB Test Case """

    def test_bfib(self):
        """ BFIB Unit Tests """
        error = self.vapi.cli("test bier")

        if error:
            self.logger.critical(error)
        self.assertEqual(error.find("Failed"), -1)


class TestBier(VppTestCase):
    """ BIER Test Case """

    def setUp(self):
        super(TestBier, self).setUp()

        # create 2 pg interfaces
        self.create_pg_interfaces(range(3))

        # create the default MPLS table
        self.tables = []
        tbl = VppMplsTable(self, 0)
        tbl.add_vpp_config()
        self.tables.append(tbl)

        tbl = VppIpTable(self, 10)
        tbl.add_vpp_config()
        self.tables.append(tbl)

        # setup both interfaces
        for i in self.pg_interfaces:
            if i == self.pg2:
                i.set_table_ip4(10)
            i.admin_up()
            i.config_ip4()
            i.resolve_arp()
            i.enable_mpls()

    def tearDown(self):
        for i in self.pg_interfaces:
            i.disable_mpls()
            i.unconfig_ip4()
            i.set_table_ip4(0)
            i.admin_down()
        super(TestBier, self).tearDown()

    def test_bier_midpoint(self):
        """BIER midpoint"""

        #
        # Add a BIER table for sub-domain 0, set 0, and BSL 256
        #
        bti = VppBierTableID(0, 0, BIERLength.BIER_LEN_256)
        bt = VppBierTable(self, bti, 77)
        bt.add_vpp_config()

        #
        # A packet with no bits set gets dropped
        #
        p = (Ether(dst=self.pg0.local_mac, src=self.pg0.remote_mac) /
             MPLS(label=77, ttl=255) /
             BIER(length=BIERLength.BIER_LEN_256,
                  BitString=chr(0)*64) /
             IPv6(src=self.pg0.remote_ip6, dst=self.pg0.remote_ip6) /
             UDP(sport=1234, dport=1234) /
             Raw())
        pkts = [p]

        self.send_and_assert_no_replies(self.pg0, pkts,
                                        "Empty Bit-String")

        #
        # Add a BIER route for each bit-position in the table via a different
        # next-hop. Testing whether the BIER walk and replicate forwarding
        # function works for all bit posisitons.
        #
        nh_routes = []
        bier_routes = []
        for i in range(1, 256):
            nh = "10.0.%d.%d" % (i / 255, i % 255)
            nh_routes.append(VppIpRoute(self, nh, 32,
                                        [VppRoutePath(self.pg1.remote_ip4,
                                                      self.pg1.sw_if_index,
                                                      labels=[2000+i])]))
            nh_routes[-1].add_vpp_config()

            bier_routes.append(VppBierRoute(self, bti, i,
                                            [VppRoutePath(nh, 0xffffffff,
                                                          labels=[100+i])]))
            bier_routes[-1].add_vpp_config()

        #
        # A packet with all bits set gets spat out to BP:1
        #
        p = (Ether(dst=self.pg0.local_mac, src=self.pg0.remote_mac) /
             MPLS(label=77, ttl=255) /
             BIER(length=BIERLength.BIER_LEN_256) /
             IPv6(src=self.pg0.remote_ip6, dst=self.pg0.remote_ip6) /
             UDP(sport=1234, dport=1234) /
             Raw())
        pkts = [p]

        self.pg0.add_stream(pkts)
        self.pg_enable_capture(self.pg_interfaces)
        self.pg_start()

        rx = self.pg1.get_capture(255)

        for rxp in rx:
            #
            # The packets are not required to be sent in bit-position order
            # when we setup the routes above we used the bit-position to
            # construct the out-label. so use that here to determine the BP
            #
            olabel = rxp[MPLS]
            bp = olabel.label - 2000

            blabel = olabel[MPLS].payload
            self.assertEqual(blabel.label, 100+bp)
            self.assertEqual(blabel.ttl, 254)

            bier_hdr = blabel[MPLS].payload

            self.assertEqual(bier_hdr.id, 5)
            self.assertEqual(bier_hdr.version, 0)
            self.assertEqual(bier_hdr.length, BIERLength.BIER_LEN_256)
            self.assertEqual(bier_hdr.entropy, 0)
            self.assertEqual(bier_hdr.OAM, 0)
            self.assertEqual(bier_hdr.RSV, 0)
            self.assertEqual(bier_hdr.DSCP, 0)
            self.assertEqual(bier_hdr.Proto, 5)

            # The bit-string should consist only of the BP given by i.
            i = 0
            bitstring = ""
            bpi = bp - 1
            while (i < bpi/8):
                bitstring = chr(0) + bitstring
                i += 1
            bitstring = chr(1 << bpi % 8) + bitstring

            while len(bitstring) < 32:
                bitstring = chr(0) + bitstring

            self.assertEqual(len(bitstring), len(bier_hdr.BitString))
            self.assertEqual(bitstring, bier_hdr.BitString)

    def test_bier_head(self):
        """BIER head"""

        #
        # Add a BIER table for sub-domain 0, set 0, and BSL 256
        #
        bti = VppBierTableID(0, 0, BIERLength.BIER_LEN_256)
        bt = VppBierTable(self, bti, 77)
        bt.add_vpp_config()

        #
        # 2 bit positions via two next hops
        #
        nh1 = "10.0.0.1"
        nh2 = "10.0.0.2"
        ip_route_1 = VppIpRoute(self, nh1, 32,
                                [VppRoutePath(self.pg1.remote_ip4,
                                              self.pg1.sw_if_index,
                                              labels=[2001])])
        ip_route_2 = VppIpRoute(self, nh2, 32,
                                [VppRoutePath(self.pg1.remote_ip4,
                                              self.pg1.sw_if_index,
                                              labels=[2002])])
        ip_route_1.add_vpp_config()
        ip_route_2.add_vpp_config()

        bier_route_1 = VppBierRoute(self, bti, 1,
                                    [VppRoutePath(nh1, 0xffffffff,
                                                  labels=[101])])
        bier_route_2 = VppBierRoute(self, bti, 2,
                                    [VppRoutePath(nh2, 0xffffffff,
                                                  labels=[102])])
        bier_route_1.add_vpp_config()
        bier_route_2.add_vpp_config()

        #
        # An imposition object with both bit-positions set
        #
        bi = VppBierImp(self, bti, 333, chr(0x3) * 32)
        bi.add_vpp_config()

        #
        # Add a multicast route that will forward into the BIER doamin
        #
        route_ing_232_1_1_1 = VppIpMRoute(
            self,
            "0.0.0.0",
            "232.1.1.1", 32,
            MRouteEntryFlags.MFIB_ENTRY_FLAG_NONE,
            paths=[VppMRoutePath(self.pg0.sw_if_index,
                                 MRouteItfFlags.MFIB_ITF_FLAG_ACCEPT),
                   VppMRoutePath(0xffffffff,
                                 MRouteItfFlags.MFIB_ITF_FLAG_FORWARD,
                                 proto=DpoProto.DPO_PROTO_BIER,
                                 bier_imp=bi.bi_index)])
        route_ing_232_1_1_1.add_vpp_config()

        #
        # inject an IP packet. We expect it to be BIER encapped and
        # replicated.
        #
        p = (Ether(dst=self.pg0.local_mac,
                   src=self.pg0.remote_mac) /
             IP(src="1.1.1.1", dst="232.1.1.1") /
             UDP(sport=1234, dport=1234))

        self.pg0.add_stream([p])
        self.pg_enable_capture(self.pg_interfaces)
        self.pg_start()

        rx = self.pg1.get_capture(2)

        #
        # Encap Stack is; eth, MPLS, MPLS, BIER
        #
        igp_mpls = rx[0][MPLS]
        self.assertEqual(igp_mpls.label, 2001)
        self.assertEqual(igp_mpls.ttl, 64)
        self.assertEqual(igp_mpls.s, 0)
        bier_mpls = igp_mpls[MPLS].payload
        self.assertEqual(bier_mpls.label, 101)
        self.assertEqual(bier_mpls.ttl, 64)
        self.assertEqual(bier_mpls.s, 1)
        self.assertEqual(rx[0][BIER].length, 2)

        igp_mpls = rx[1][MPLS]
        self.assertEqual(igp_mpls.label, 2002)
        self.assertEqual(igp_mpls.ttl, 64)
        self.assertEqual(igp_mpls.s, 0)
        bier_mpls = igp_mpls[MPLS].payload
        self.assertEqual(bier_mpls.label, 102)
        self.assertEqual(bier_mpls.ttl, 64)
        self.assertEqual(bier_mpls.s, 1)
        self.assertEqual(rx[0][BIER].length, 2)

    def test_bier_tail(self):
        """BIER Tail"""

        #
        # Add a BIER table for sub-domain 0, set 0, and BSL 256
        #
        bti = VppBierTableID(0, 0, BIERLength.BIER_LEN_256)
        bt = VppBierTable(self, bti, 77)
        bt.add_vpp_config()

        #
        # disposition table
        #
        bdt = VppBierDispTable(self, 8)
        bdt.add_vpp_config()

        #
        # BIER route in table that's for-us
        #
        bier_route_1 = VppBierRoute(self, bti, 1,
                                    [VppRoutePath("0.0.0.0",
                                                  0xffffffff,
                                                  nh_table_id=8)])
        bier_route_1.add_vpp_config()

        #
        # An entry in the disposition table
        #
        bier_de_1 = VppBierDispEntry(self, bdt.id, 99,
                                     BIER_HDR_PAYLOAD.BIER_HDR_PROTO_IPV4,
                                     "0.0.0.0", 0, rpf_id=8192)
        bier_de_1.add_vpp_config()

        #
        # A multicast route to forward post BIER disposition
        #
        route_eg_232_1_1_1 = VppIpMRoute(
            self,
            "0.0.0.0",
            "232.1.1.1", 32,
            MRouteEntryFlags.MFIB_ENTRY_FLAG_NONE,
            paths=[VppMRoutePath(self.pg1.sw_if_index,
                                 MRouteItfFlags.MFIB_ITF_FLAG_FORWARD)])
        route_eg_232_1_1_1.add_vpp_config()
        route_eg_232_1_1_1.update_rpf_id(8192)

        #
        # A packet with all bits set gets spat out to BP:1
        #
        p = (Ether(dst=self.pg0.local_mac, src=self.pg0.remote_mac) /
             MPLS(label=77, ttl=255) /
             BIER(length=BIERLength.BIER_LEN_256, BFRID=99) /
             IP(src="1.1.1.1", dst="232.1.1.1") /
             UDP(sport=1234, dport=1234) /
             Raw())

        self.send_and_expect(self.pg0, [p], self.pg1)

        #
        # A packet that does not match the Disposition entry gets dropped
        #
        p = (Ether(dst=self.pg0.local_mac, src=self.pg0.remote_mac) /
             MPLS(label=77, ttl=255) /
             BIER(length=BIERLength.BIER_LEN_256, BFRID=77) /
             IP(src="1.1.1.1", dst="232.1.1.1") /
             UDP(sport=1234, dport=1234) /
             Raw())
        self.send_and_assert_no_replies(self.pg0, p*2,
                                        "no matching disposition entry")

        #
        # Add the default route to the disposition table
        #
        bier_de_2 = VppBierDispEntry(self, bdt.id, 0,
                                     BIER_HDR_PAYLOAD.BIER_HDR_PROTO_IPV4,
                                     "0.0.0.0", 0, rpf_id=8192)
        bier_de_2.add_vpp_config()

        #
        # now the previous packet is forwarded
        #
        self.send_and_expect(self.pg0, [p], self.pg1)

    def test_bier_e2e(self):
        """ BIER end-to-end """

        #
        # Add a BIER table for sub-domain 0, set 0, and BSL 256
        #
        bti = VppBierTableID(0, 0, BIERLength.BIER_LEN_256)
        bt = VppBierTable(self, bti, 77)
        bt.add_vpp_config()

        #
        # Impostion Sets bit string 101010101....
        #  sender 333
        #
        bi = VppBierImp(self, bti, 333, chr(0x5) * 32)
        bi.add_vpp_config()

        #
        # Add a multicast route that will forward into the BIER doamin
        #
        route_ing_232_1_1_1 = VppIpMRoute(
            self,
            "0.0.0.0",
            "232.1.1.1", 32,
            MRouteEntryFlags.MFIB_ENTRY_FLAG_NONE,
            paths=[VppMRoutePath(self.pg0.sw_if_index,
                                 MRouteItfFlags.MFIB_ITF_FLAG_ACCEPT),
                   VppMRoutePath(0xffffffff,
                                 MRouteItfFlags.MFIB_ITF_FLAG_FORWARD,
                                 proto=DpoProto.DPO_PROTO_BIER,
                                 bier_imp=bi.bi_index)])
        route_ing_232_1_1_1.add_vpp_config()

        #
        # disposition table 8
        #
        bdt = VppBierDispTable(self, 8)
        bdt.add_vpp_config()

        #
        # BIER route in table that's for-us, resolving through
        # disp table 8.
        #
        bier_route_1 = VppBierRoute(self, bti, 1,
                                    [VppRoutePath("0.0.0.0",
                                                  0xffffffff,
                                                  nh_table_id=8)])
        bier_route_1.add_vpp_config()

        #
        # An entry in the disposition table for sender 333
        #  lookup in VRF 10
        #
        bier_de_1 = VppBierDispEntry(self, bdt.id, 333,
                                     BIER_HDR_PAYLOAD.BIER_HDR_PROTO_IPV4,
                                     "0.0.0.0", 10, rpf_id=8192)
        bier_de_1.add_vpp_config()

        #
        # Add a multicast route that will forward the traffic
        # post-disposition
        #
        route_eg_232_1_1_1 = VppIpMRoute(
            self,
            "0.0.0.0",
            "232.1.1.1", 32,
            MRouteEntryFlags.MFIB_ENTRY_FLAG_NONE,
            table_id=10,
            paths=[VppMRoutePath(self.pg1.sw_if_index,
                                 MRouteItfFlags.MFIB_ITF_FLAG_FORWARD)])
        route_eg_232_1_1_1.add_vpp_config()
        route_eg_232_1_1_1.update_rpf_id(8192)

        #
        # inject a packet in VRF-0. We expect it to be BIER encapped,
        # replicated, then hit the disposition and be forwarded
        # out of VRF 10, i.e. on pg1
        #
        p = (Ether(dst=self.pg0.local_mac,
                   src=self.pg0.remote_mac) /
             IP(src="1.1.1.1", dst="232.1.1.1") /
             UDP(sport=1234, dport=1234))

        rx = self.send_and_expect(self.pg0, p*65, self.pg1)

        #
        # should be IP
        #
        self.assertEqual(rx[0][IP].src, "1.1.1.1")
        self.assertEqual(rx[0][IP].dst, "232.1.1.1")

    def test_bier_head_o_udp(self):
        """BIER head over UDP"""

        #
        # Add a BIER table for sub-domain 1, set 0, and BSL 256
        #
        bti = VppBierTableID(1, 0, BIERLength.BIER_LEN_256)
        bt = VppBierTable(self, bti, 77)
        bt.add_vpp_config()

        #
        # 1 bit positions via 1 next hops
        #
        nh1 = "10.0.0.1"
        ip_route = VppIpRoute(self, nh1, 32,
                              [VppRoutePath(self.pg1.remote_ip4,
                                            self.pg1.sw_if_index,
                                            labels=[2001])])
        ip_route.add_vpp_config()

        udp_encap = VppUdpEncap(self, 4,
                                self.pg0.local_ip4,
                                nh1,
                                330, 8138)
        udp_encap.add_vpp_config()

        bier_route = VppBierRoute(self, bti, 1,
                                  [VppRoutePath("0.0.0.0",
                                                0xFFFFFFFF,
                                                is_udp_encap=1,
                                                next_hop_id=4)])
        bier_route.add_vpp_config()

        #
        # An 2 imposition objects with all bit-positions set
        # only use the second, but creating 2 tests with a non-zero
        # value index in the route add
        #
        bi = VppBierImp(self, bti, 333, chr(0xff) * 32)
        bi.add_vpp_config()
        bi2 = VppBierImp(self, bti, 334, chr(0xff) * 32)
        bi2.add_vpp_config()

        #
        # Add a multicast route that will forward into the BIER doamin
        #
        route_ing_232_1_1_1 = VppIpMRoute(
            self,
            "0.0.0.0",
            "232.1.1.1", 32,
            MRouteEntryFlags.MFIB_ENTRY_FLAG_NONE,
            paths=[VppMRoutePath(self.pg0.sw_if_index,
                                 MRouteItfFlags.MFIB_ITF_FLAG_ACCEPT),
                   VppMRoutePath(0xffffffff,
                                 MRouteItfFlags.MFIB_ITF_FLAG_FORWARD,
                                 proto=DpoProto.DPO_PROTO_BIER,
                                 bier_imp=bi2.bi_index)])
        route_ing_232_1_1_1.add_vpp_config()

        #
        # inject a packet an IP. We expect it to be BIER and UDP encapped,
        #
        p = (Ether(dst=self.pg0.local_mac,
                   src=self.pg0.remote_mac) /
             IP(src="1.1.1.1", dst="232.1.1.1") /
             UDP(sport=1234, dport=1234))

        self.pg0.add_stream([p])
        self.pg_enable_capture(self.pg_interfaces)
        self.pg_start()

        rx = self.pg1.get_capture(1)

        #
        # Encap Stack is, eth, IP, UDP, BIFT, BIER
        #
        self.assertEqual(rx[0][IP].src, self.pg0.local_ip4)
        self.assertEqual(rx[0][IP].dst, nh1)
        self.assertEqual(rx[0][UDP].sport, 330)
        self.assertEqual(rx[0][UDP].dport, 8138)
        self.assertEqual(rx[0][BIFT].bsl, 2)
        self.assertEqual(rx[0][BIFT].sd, 1)
        self.assertEqual(rx[0][BIFT].set, 0)
        self.assertEqual(rx[0][BIFT].ttl, 64)
        self.assertEqual(rx[0][BIER].length, 2)

    def test_bier_tail_o_udp(self):
        """BIER Tail over UDP"""

        #
        # Add a BIER table for sub-domain 0, set 0, and BSL 256
        #
        bti = VppBierTableID(1, 0, BIERLength.BIER_LEN_256)
        bt = VppBierTable(self, bti, MPLS_LABEL_INVALID)
        bt.add_vpp_config()

        #
        # disposition table
        #
        bdt = VppBierDispTable(self, 8)
        bdt.add_vpp_config()

        #
        # BIER route in table that's for-us
        #
        bier_route_1 = VppBierRoute(self, bti, 1,
                                    [VppRoutePath("0.0.0.0",
                                                  0xffffffff,
                                                  nh_table_id=8)])
        bier_route_1.add_vpp_config()

        #
        # An entry in the disposition table
        #
        bier_de_1 = VppBierDispEntry(self, bdt.id, 99,
                                     BIER_HDR_PAYLOAD.BIER_HDR_PROTO_IPV4,
                                     "0.0.0.0", 0, rpf_id=8192)
        bier_de_1.add_vpp_config()

        #
        # A multicast route to forward post BIER disposition
        #
        route_eg_232_1_1_1 = VppIpMRoute(
            self,
            "0.0.0.0",
            "232.1.1.1", 32,
            MRouteEntryFlags.MFIB_ENTRY_FLAG_NONE,
            paths=[VppMRoutePath(self.pg1.sw_if_index,
                                 MRouteItfFlags.MFIB_ITF_FLAG_FORWARD)])
        route_eg_232_1_1_1.add_vpp_config()
        route_eg_232_1_1_1.update_rpf_id(8192)

        #
        # A packet with all bits set gets spat out to BP:1
        #
        p = (Ether(dst=self.pg0.local_mac, src=self.pg0.remote_mac) /
             IP(src=self.pg0.remote_ip4, dst=self.pg0.local_ip4) /
             UDP(sport=333, dport=8138) /
             BIFT(sd=1, set=0, bsl=2, ttl=255) /
             BIER(length=BIERLength.BIER_LEN_256, BFRID=99) /
             IP(src="1.1.1.1", dst="232.1.1.1") /
             UDP(sport=1234, dport=1234) /
             Raw())

        rx = self.send_and_expect(self.pg0, [p], self.pg1)


if __name__ == '__main__':
    unittest.main(testRunner=VppTestRunner)
