from multiaddr import Multiaddr
# construct from a string
m1 = Multiaddr("/ip4/127.0.0.1/udp/1234")
m2 =  Multiaddr("/unix/run/user/1000/sway-ipc.1000.1557.sock")
for key in m1.protocols():
    key
    
uds_sock_path = Path(m2.values()[0])
uds_sock_path
uds_sock_path.is_file()
uds_sock_path.is_socket()
