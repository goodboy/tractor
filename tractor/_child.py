import sys
import trio
import cloudpickle

if __name__ == "__main__":
    job = cloudpickle.load(sys.stdin.detach())

    try:
        result = trio.run(job)
        cloudpickle.dump(sys.stdout.detach(), result)

    except BaseException as err:
        cloudpickle.dump(sys.stdout.detach(), err)