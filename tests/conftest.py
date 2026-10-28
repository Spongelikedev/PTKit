"""Shared fixtures.

The synthetic documents below mimic real Packet Tracer XML, including the
serializer quirks the codec must preserve: raw '>' in text, a space before
'>' in a start tag, <X></X> vs <X/>, CDATA, a C0 control character and a
trailing newline. Serial ports are placed before FastEthernet ports in the
XML (as on real 1841s with a WIC) to exercise per-family port naming.
"""

import os
from pathlib import Path

import pytest

ROUTER = """\
   <DEVICE>
    <ENGINE>
     <TYPE model="1841" >Router</TYPE>
     <NAME translate="true">Router0</NAME>
     <POWER>true</POWER>
     <DESCRIPTION></DESCRIPTION>
     <MODULE>
      <TYPE>eNonRemovableModule</TYPE>
      <SLOT>
       <MODULE>
        <TYPE>eInterfaceCard</TYPE>
        <PORT>
         <TYPE>eSmartSerial</TYPE>
         <POWER>true</POWER>
         <IP>10.0.0.1</IP>
         <SUBNET>255.255.255.252</SUBNET>
        </PORT>
        <PORT>
         <TYPE>eSmartSerial</TYPE>
         <POWER>false</POWER>
         <IP/>
         <SUBNET/>
        </PORT>
       </MODULE>
      </SLOT>
      <PORT>
       <TYPE>eCopperFastEthernet</TYPE>
       <POWER>true</POWER>
       <MACADDRESS>0001.4207.3B01</MACADDRESS>
       <IP>192.168.1.1</IP>
       <SUBNET>255.255.255.0</SUBNET>
      </PORT>
      <PORT>
       <TYPE>eCopperFastEthernet</TYPE>
       <POWER>false</POWER>
       <MACADDRESS>0001.4207.3B02</MACADDRESS>
       <IP/>
       <SUBNET/>
      </PORT>
     </MODULE>
     <SYS_NAME>Router</SYS_NAME>
     <RUNNINGCONFIG>
      <LINE>!</LINE>
      <LINE>version 12.4</LINE>
      <LINE>no service password-encryption</LINE>
      <LINE>!</LINE>
      <LINE>hostname Router</LINE>
      <LINE>!</LINE>
      <LINE>interface FastEthernet0/0</LINE>
      <LINE> ip address 192.168.1.1 255.255.255.0</LINE>
      <LINE> duplex auto</LINE>
      <LINE> speed auto</LINE>
      <LINE>!</LINE>
      <LINE>interface FastEthernet0/1</LINE>
      <LINE> no ip address</LINE>
      <LINE> duplex auto</LINE>
      <LINE> speed auto</LINE>
      <LINE> shutdown</LINE>
      <LINE>!</LINE>
      <LINE>interface Serial0/0/0</LINE>
      <LINE> ip address 10.0.0.1 255.255.255.252</LINE>
      <LINE> clock rate 64000</LINE>
      <LINE>!</LINE>
      <LINE>interface Serial0/0/1</LINE>
      <LINE> no ip address</LINE>
      <LINE> shutdown</LINE>
      <LINE>!</LINE>
      <LINE>interface Vlan1</LINE>
      <LINE> no ip address</LINE>
      <LINE> shutdown</LINE>
      <LINE>!</LINE>
      <LINE>line con 0</LINE>
      <LINE>!</LINE>
      <LINE>end</LINE>
     </RUNNINGCONFIG>
     <STARTUPCONFIG/>
     <SAVE_REF_ID>save-ref-id:1001</SAVE_REF_ID>
    </ENGINE>
    <WORKSPACE>
     <LOGICAL>
      <X>100</X>
      <Y>200</Y>
     </LOGICAL>
    </WORKSPACE>
   </DEVICE>
"""

PC = """\
   <DEVICE>
    <ENGINE>
     <TYPE customModel="" model="PC-PT">Pc</TYPE>
     <NAME translate="true">PC0</NAME>
     <POWER>true</POWER>
     <DESCRIPTION></DESCRIPTION>
     <MODULE>
      <TYPE>eNonRemovableModule</TYPE>
      <PORT>
       <TYPE>eCopperFastEthernet</TYPE>
       <POWER>true</POWER>
       <IP>192.168.1.10</IP>
       <SUBNET>255.255.255.0</SUBNET>
       <PORT_DHCP_ENABLE>true</PORT_DHCP_ENABLE>
      </PORT>
     </MODULE>
     <SYS_NAME>PC</SYS_NAME>
     <GATEWAY>192.168.1.1</GATEWAY>
     <SAVE_REF_ID>save-ref-id:1002</SAVE_REF_ID>
     <SCRIPT_DATA>
      <CONTENT><![CDATA[if (a > b && c < d) { x(); }]]></CONTENT>
     </SCRIPT_DATA>
     <HISTORY>ping 1.1.1.1\x03exit</HISTORY>
     <NOTE>a > b &amp; c</NOTE>
    </ENGINE>
   </DEVICE>
"""

NETWORK = """\
 <VERSION>8.2.2.0400</VERSION>
 <NETWORK>
  <DEVICES>
{router}{pc}  </DEVICES>
  <LINKS>
   <LINK>
    <TYPE>eCopper</TYPE>
    <CABLE>
     <FROM>save-ref-id:1001</FROM>
     <PORT>FastEthernet0/0</PORT>
     <TO>save-ref-id:1002</TO>
     <PORT>FastEthernet0</PORT>
     <TYPE>eStraightThrough</TYPE>
    </CABLE>
   </LINK>
  </LINKS>
 </NETWORK>
 <PHYSICALWORKSPACE>
  <NODE>
   <TYPE>0</TYPE>
   <NAME>Intercity</NAME>
   <CHILDREN>
    <NODE>
     <TYPE>6</TYPE>
     <NAME>Router0</NAME>
     <UUID_STR>{{00000000-0000-0000-0000-000000000001}}</UUID_STR>
    </NODE>
    <NODE>
     <TYPE>6</TYPE>
     <NAME>PC0</NAME>
     <UUID_STR>{{00000000-0000-0000-0000-000000000002}}</UUID_STR>
    </NODE>
   </CHILDREN>
  </NODE>
 </PHYSICALWORKSPACE>
"""


def make_pkt_xml() -> bytes:
    body = NETWORK.format(router=ROUTER, pc=PC)
    return ("<PACKETTRACER5>\n" + body + "</PACKETTRACER5>\n").encode("utf-8")


def make_pka_xml(*, password: str = "", pt4: bool = False) -> bytes:
    """Three-network activity whose answer network is marked (hostname
    ANSWER-KEY). ``password`` sets ACTIVITY@PASS (author lock); ``pt4`` uses
    PT 4.x element names plus 4.x quirks: a &#x3; character reference and a
    NUL byte after the root element."""
    net = NETWORK.format(router=ROUTER, pc=PC)
    if pt4:
        net = net.replace("<NOTE>a > b &amp; c</NOTE>", "<NOTE>a&#x3;b</NOTE>")
    answer = (net.replace("hostname Router<", "hostname ANSWER-KEY<")
              .replace("<SYS_NAME>Router</SYS_NAME>", "<SYS_NAME>ANSWER-KEY</SYS_NAME>"))
    tag = "PACKETTRACER" if pt4 else "PACKETTRACER5"
    nets = "".join(f" <{tag}>\n{n} </{tag}>\n" for n in (net, net, answer))
    tail = """\
 <COMPARISONS>
  <NODE>
   <NAME checkType="1" headNode="true">Network</NAME>
   <ID>Network</ID>
   <COMPONENTS/>
   <POINTS/>
   <NODE>
    <NAME checkType="0" headNode="true">Router0</NAME>
    <ID>Router0</ID>
    <COMPONENTS/>
    <POINTS/>
    <NODE>
     <NAME checkType="2" headNode="false" nodeValue="SECRET-HOSTNAME">Host Name</NAME>
     <ID>HostName</ID>
     <COMPONENTS>Other</COMPONENTS>
     <POINTS>2</POINTS>
    </NODE>
    <NODE>
     <NAME checkType="0" headNode="false" nodeValue="SECRET-POWER">Power</NAME>
     <ID>Power</ID>
     <COMPONENTS>Physical</COMPONENTS>
     <POINTS>1</POINTS>
    </NODE>
    <NODE>
     <NAME checkType="1" headNode="true">Ports</NAME>
     <ID>Ports</ID>
     <COMPONENTS/>
     <POINTS/>
     <NODE>
      <NAME checkType="1" headNode="true">FastEthernet0/0</NAME>
      <ID>FastEthernet0/0</ID>
      <COMPONENTS/>
      <POINTS/>
      <NODE>
       <NAME checkType="2" headNode="false" nodeValue="SECRET-10.9.9.9">IP Address</NAME>
       <ID>IpAddress</ID>
       <COMPONENTS>Connectivity</COMPONENTS>
       <POINTS>3</POINTS>
      </NODE>
     </NODE>
    </NODE>
    <NODE>
     <NAME checkType="2" headNode="false" obfuscateName="true" nodeValue="SECRET-X">Secret Item</NAME>
     <ID>Hidden</ID>
     <COMPONENTS>Other</COMPONENTS>
     <POINTS>1</POINTS>
    </NODE>
   </NODE>
  </NODE>
 </COMPARISONS>
 <ACTIVITY ENABLED="no" PASS="">
  <INSTRUCTIONS layout="0">
   <PAGE translate="true" title="">Configure &lt;b&gt;Router0&lt;/b&gt;.</PAGE>
  </INSTRUCTIONS>
 </ACTIVITY>
"""
    tail = tail.replace('PASS=""', f'PASS="{password}"')
    root = "PACKETTRACER_ACTIVITY" if pt4 else "PACKETTRACER5_ACTIVITY"
    version = "4.1" if pt4 else "8.2.2.0400"
    xml = (f"<{root}>\n <VERSION>{version}</VERSION>\n" + nets + tail
           + f"</{root}>\n").encode("utf-8")
    return xml + b"\x00" if pt4 else xml


@pytest.fixture
def pkt_xml() -> bytes:
    return make_pkt_xml()


@pytest.fixture
def pka_xml() -> bytes:
    return make_pka_xml()


def sample_files() -> list[Path]:
    """Sample files from a local Packet Tracer install (PTKIT_SAMPLES overrides)."""
    roots = [os.environ.get("PTKIT_SAMPLES")] if os.environ.get("PTKIT_SAMPLES") else []
    pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    roots += [str(p / "saves") for p in sorted(pf.glob("Cisco Packet Tracer*"))]
    for r in roots:
        files = sorted(Path(r).rglob("*.pk[ta]"))
        if files:
            return files
    return []
