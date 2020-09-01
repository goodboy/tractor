from pytest_vnet import run_in_netvm

@run_in_netvm
def test_remote_actor_simple():

    switch = vnet.addSwitch('s0')

    @as_host(vnet, 'h1', switch, ip='10.0.0.1')
    def daemon():
        import tractor
        tractor.run_daemon(
            (), arbiter_addr=daemon_addr
        )

    @as_host(vnet, 'h2', switch, ip='10.0.0.1')
    def client():
        import time
        import tractor

        async def main():
            async with tractor.get_arbiter(daemon_addr) as portal:
                await portal.cancel_actor()

            time.sleep(0.1)

            # no arbiter socket should exist
            try:
                async with tractor.get_arbiter(daemon_addr) as portal:
                    assert False  # this shouldn't run
            except OSError:
                pass

        tractor.run(main)

    vnet.start()
    daemon.start_host()
    client.start_host()
    client.proc.wait(timeout=3)

@run_in_netvm
def test_internet_hello():
    """
       'internet'
       server (h0)
           |
       switch (s0)
           |
    ----------------
    |              |
   isp 1          isp 2
   (nat1)         (nat2)
    |              |
  switch 1       switch 2
   (s1)           (s2)
    |              |
  client 1       client 2
   (h1)           (h2)

    """

    switch_inet = vnet.addSwitch('s0')
   
    @as_host(vnet, 'h0', switch_inet)
    def motd_server():
        import tractor

        async def get_motd():
            return "Hello intranet friends!"

        tractor.run_daemon(
            (__file__), arbiter_addr=daemon_addr
        )

    clients = []

    for i in range(1, 3):
        inet_iface = f"nat_{i}-eth0"
        local_iface = f"nat_{i}-eth1"
        local_addr = f"192.168.{i}.1"
        local_subnet = f"192.168.{i}.0/24"
        nat_params = { 'ip' : f"{local_addr}/24" }

        vnet.ipBase = local_subnet
        # ^ needed to overwrite default subnet passed to addNAT
        nat = vnet.addNAT(
            f"nat{i}",
            inetIntf=inet_iface,
            localIntf=local_iface
        )

        switch = vnet.addSwitch(f"s{i}")
        vnet.addLink(nat, switch_inet, intfName1=inet_iface)
        vnet.addLink(nat, switch, intfName1=local_iface, params1=nat_params)

        @as_host(
            vnet, f"h{i}", switch,
            ip=f"192.168.{i}.100/24",
            defaultRoute=f"via {local_addr}"
        )
        def motd_client():
            import tractor

            async def main():
                async with tractor.get_arbiter('10.0.0.1') as portal:
                    assert await portal.get_motd() == "Hello intranet friends!"

            tractor.run(main)

        clients.append(motd_client)

    vnet.start()
    motd_server.start_host()
    for client in clients:
        client.start_host()

    for client in clients:
        client.proc.wait(timeout=5)