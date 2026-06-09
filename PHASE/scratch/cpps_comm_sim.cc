/*
 * CPPS Comm — BA scale-free + OLSR dynamic routing (50 nodes).
 */
#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/point-to-point-module.h"
#include "ns3/applications-module.h"
#include "ns3/flow-monitor-module.h"
#include "ns3/olsr-helper.h"
#include "ns3/ipv4-static-routing-helper.h"
#include "ns3/ipv4-list-routing-helper.h"
#include <fstream>
#include <sstream>
#include <set>
#include <vector>
#include <map>

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("CppsCommOlsr");

int main(int argc, char *argv[]) {
    std::string cfg = "cpps_comm_config.txt";
    std::string oxml = "flowmon-output.xml", ocsv = "comm_status.csv";

    std::ifstream f(cfg);
    if (!f.is_open()) { std::cerr << "no config\n"; return 1; }
    int n_nodes = 0, n_edges = 0; double sim_time = 1.0; int sim_mode = 0;
    std::vector<std::pair<uint32_t, uint32_t>> edges;
    std::set<uint32_t> active, failed;
    std::string line, key;
    while (std::getline(f, line)) {
        if (line.empty() || line[0] == '#') continue;
        std::istringstream iss(line); iss >> key;
        if (key == "n_nodes") iss >> n_nodes;
        else if (key == "n_edges") iss >> n_edges;
        else if (key == "sim_time") iss >> sim_time;
        else if (key == "sim_mode") iss >> sim_mode;
        else if (key == "edge") { uint32_t a, b; iss >> a >> b; edges.push_back({a, b}); }
        else if (key == "active_nodes") {
            int v; while (iss >> v) active.insert((uint32_t)v); }
        else if (key == "failed_nodes") {
            int v; while (iss >> v) failed.insert((uint32_t)v); }
    }
    f.close();
    for (auto n : failed) active.erase(n);

    NodeContainer nodes; nodes.Create(n_nodes);

    // ---- Step 1: Install links (only between active nodes) ----
    // Reduced bandwidth (10Mbps) to create realistic congestion with background traffic
    PointToPointHelper p2p;
    p2p.SetDeviceAttribute("DataRate", StringValue("10Mbps"));
    p2p.SetChannelAttribute("Delay", StringValue("5ms"));

    std::set<uint32_t> routed; // nodes with at least one interface
    std::vector<NetDeviceContainer> devs;
    std::vector<std::pair<uint32_t, uint32_t>> installed;
    for (auto &e : edges) {
        uint32_t a = e.first, b = e.second;
        if (failed.count(a) || failed.count(b)) continue;
        if (!active.count(a) || !active.count(b)) continue;
        devs.push_back(p2p.Install(nodes.Get(a), nodes.Get(b)));
        installed.push_back({a, b});
        routed.insert(a); routed.insert(b);
    }

    // ---- Step 2: Internet + OLSR only on routed nodes ----
    NodeContainer rnodes;
    for (auto n : routed) rnodes.Add(nodes.Get(n));

    // Configure OLSR with shorter intervals for 1-2s simulation steps
    OlsrHelper olsr;
    olsr.Set("HelloInterval", TimeValue(Seconds(0.3)));
    olsr.Set("TcInterval", TimeValue(Seconds(0.6)));

    InternetStackHelper internet;
    Ipv4ListRoutingHelper list;
    list.Add(olsr, 100);
    internet.SetRoutingHelper(list);
    internet.Install(rnodes);

    // ---- Step 3: IP addressing ----
    Ipv4AddressHelper addr;
    std::map<std::pair<uint32_t, uint32_t>, Ipv4InterfaceContainer> ifaceMap;
    for (size_t i = 0; i < devs.size(); i++) {
        auto &e = installed[i];
        std::ostringstream s;
        s << "10." << (e.first % 256) << "." << (e.second % 256) << ".0";
        addr.SetBase(s.str().c_str(), "255.255.255.0");
        ifaceMap[e] = addr.Assign(devs[i]);
    }

    // ---- Step 4: Node 0 IP ----
    Ipv4Address a0;
    for (auto &kv : ifaceMap) {
        if (kv.first.first == 0) { a0 = kv.second.GetAddress(0); break; }
        if (kv.first.second == 0) { a0 = kv.second.GetAddress(1); break; }
    }
    if (a0 == Ipv4Address()) { std::cerr << "Node0 has no IP\n"; return 1; }

    // ---- Step 5: Traffic (RTU nodes -> control center 0) ----
    int n_power_comm = n_nodes - 5;  // power comm nodes = total - relays
    uint16_t bp = 1000;
    for (uint32_t rtu = 1; rtu < (uint32_t)n_nodes && rtu < (uint32_t)n_power_comm; rtu++) {
        if (!active.count(rtu) || failed.count(rtu)) continue;
        if (!routed.count(rtu)) continue;

        uint16_t up = bp + rtu * 2;
        OnOffHelper up_o("ns3::UdpSocketFactory", InetSocketAddress(a0, up));
        up_o.SetConstantRate(DataRate("500kbps"));
        up_o.SetAttribute("PacketSize", UintegerValue(1024));  // larger packets
        ApplicationContainer upa = up_o.Install(nodes.Get(rtu));
        upa.Start(Seconds(0.8)); upa.Stop(Seconds(sim_time - 0.2));

        PacketSinkHelper ups("ns3::UdpSocketFactory",
                             InetSocketAddress(Ipv4Address::GetAny(), up));
        ApplicationContainer uapp = ups.Install(nodes.Get(0));
        uapp.Start(Seconds(0.0));
    }

    // Downlink: control center -> each RTU (command channel @ 200kbps)
    for (uint32_t rtu = 1; rtu < (uint32_t)n_nodes && rtu < (uint32_t)n_power_comm; rtu++) {
        if (!active.count(rtu) || failed.count(rtu)) continue;
        if (!routed.count(rtu)) continue;
        // Find RTU IP
        Ipv4Address rtuIp;
        for (auto &kv : ifaceMap) {
            if (kv.first.first == rtu) { rtuIp = kv.second.GetAddress(0); break; }
            if (kv.first.second == rtu) { rtuIp = kv.second.GetAddress(1); break; }
        }
        if (rtuIp == Ipv4Address()) continue;
        uint16_t dp = bp + rtu * 2 + 1;
        OnOffHelper dof("ns3::UdpSocketFactory", InetSocketAddress(rtuIp, dp));
        dof.SetConstantRate(DataRate("200kbps"));
        dof.SetAttribute("PacketSize", UintegerValue(128));
        ApplicationContainer dapp = dof.Install(nodes.Get(0));
        dapp.Start(Seconds(0.9)); dapp.Stop(Seconds(sim_time - 0.2));
        PacketSinkHelper ds("ns3::UdpSocketFactory", InetSocketAddress(Ipv4Address::GetAny(), dp));
        ApplicationContainer dsapp = ds.Install(nodes.Get(rtu));
        dsapp.Start(Seconds(0.0));
    }

    // Background traffic: random pairs to create link congestion
    std::srand(static_cast<unsigned>(sim_time * 1000));
    for (uint32_t i = 0; i < 20; i++) {
        uint32_t src = std::rand() % n_nodes, dst = std::rand() % n_nodes;
        if (src == dst || !active.count(src) || !active.count(dst)) continue;
        if (!routed.count(src) || !routed.count(dst)) continue;
        Ipv4Address dstIp;
        for (auto &kv : ifaceMap) {
            if (kv.first.first == dst) { dstIp = kv.second.GetAddress(0); break; }
            if (kv.first.second == dst) { dstIp = kv.second.GetAddress(1); break; }
        }
        if (dstIp == Ipv4Address()) continue;
        uint16_t bgp = 5000 + i;
        OnOffHelper bg("ns3::UdpSocketFactory", InetSocketAddress(dstIp, bgp));
        bg.SetConstantRate(DataRate("200kbps"));
        bg.SetAttribute("PacketSize", UintegerValue(1500));
        ApplicationContainer bga = bg.Install(nodes.Get(src));
        bga.Start(Seconds(0.3)); bga.Stop(Seconds(sim_time - 0.1));
        PacketSinkHelper bgs("ns3::UdpSocketFactory", InetSocketAddress(Ipv4Address::GetAny(), bgp));
        ApplicationContainer bgsapp = bgs.Install(nodes.Get(dst));
        bgsapp.Start(Seconds(0.0));
    }

// Per-bus periodic status reporting (sim_mode=1)    if (sim_mode == 1) {        uint16_t rpt_base = 3000;        for (uint32_t bus = 1; bus <= 32; bus++) {            uint32_t rtu = (bus % 14) + 1; // map bus to RTU            if (!active.count(rtu) || failed.count(rtu)) continue;            if (!routed.count(rtu)) continue;            uint16_t rpt_port = rpt_base + bus;            OnOffHelper rpt("ns3::UdpSocketFactory", InetSocketAddress(a0, rpt_port));            rpt.SetAttribute("OnTime", StringValue("ns3::ConstantRandomVariable[Constant=0.01]"));            rpt.SetAttribute("OffTime", StringValue("ns3::ConstantRandomVariable[Constant=0.49]"));            rpt.SetAttribute("PacketSize", UintegerValue(200));            rpt.SetAttribute("DataRate", DataRateValue(DataRate("32kbps")));            ApplicationContainer rpt_a = rpt.Install(nodes.Get(rtu));            rpt_a.Start(Seconds(0.3)); rpt_a.Stop(Seconds(sim_time - 0.1));            PacketSinkHelper rpt_s("ns3::UdpSocketFactory", InetSocketAddress(Ipv4Address::GetAny(), rpt_port));            ApplicationContainer rpt_sa = rpt_s.Install(nodes.Get(0));            rpt_sa.Start(Seconds(0.0));        }    }
    // ---- Step 6: Run ----
    Ptr<FlowMonitor> fm; FlowMonitorHelper fh; fm = fh.InstallAll();
    Simulator::Stop(Seconds(sim_time)); Simulator::Run();

    // ---- Step 7: Output ----
    fm->CheckForLostPackets();
    Ptr<Ipv4FlowClassifier> cls = DynamicCast<Ipv4FlowClassifier>(fh.GetClassifier());
    auto st = fm->GetFlowStats();
    std::ofstream csv(ocsv);
    csv << "flow_id,src_ip,dst_ip,tx_pkts,rx_pkts,lost,"
        << "throughput_mbps,mean_delay_ms,reachable\n";
    for (auto &kv : st) {
        auto t = cls->FindFlow(kv.first); auto &s = kv.second;
        uint64_t lo = (s.txPackets > s.rxPackets) ? (s.txPackets - s.rxPackets) : 0;
        double dur = s.timeLastRxPacket.GetSeconds() - s.timeFirstTxPacket.GetSeconds();
        double tp = (dur > 0) ? s.rxBytes * 8.0 / dur / 1e6 : 0.0;
        double dly = s.rxPackets > 0 ? s.delaySum.GetSeconds() / s.rxPackets * 1000 : -1.0;
        csv << kv.first << "," << t.sourceAddress << "," << t.destinationAddress << ","
            << s.txPackets << "," << s.rxPackets << "," << lo << ","
            << tp << "," << dly << "," << (s.rxPackets > 0 ? 1 : 0) << "\n";
    }
    csv.close();
    fm->SerializeToXmlFile(oxml, true, true);
    Simulator::Destroy();
    return 0;
}
