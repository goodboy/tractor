import sys
import trio
import cloudpickle

if __name__ == "__main__":
    trio.run(cloudpickle.load(sys.stdin.buffer))
