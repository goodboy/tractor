from pytest_vnet import run_in_netvm

@run_in_netvm
def test_remote_actor_simple():

    s3 = vnet.addSwitch('s3')

    @as_host(vnet, 'h1', '10.0.0.1', s3)
    def daemon():
        import tractor
        tractor.run_daemon(
            (), arbiter_addr=daemon_addr
        )

    @as_host(vnet, 'h2', '10.0.0.1', s3)
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