'''
The pure-stdlib `concurrent.futures.ProcessPoolExecutor`
primes demo (from the std docs) verbatim; the baseline twin
of `concurrent_actors_primes.py`.

The `async def main()` + `trio.run()` shim at the bottom only
exists so the docs-example test runner can exercise this
script; the executor code itself is untouched stdlib fare.

'''
import time
import concurrent.futures
import math

import trio

PRIMES = [
    112272535095293,
    112582705942171,
    112272535095293,
    115280095190773,
    115797848077099,
    1099726899285419]


def is_prime(n):
    if n < 2:
        return False
    if n == 2:
        return True
    if n % 2 == 0:
        return False

    sqrt_n = int(math.floor(math.sqrt(n)))
    for i in range(3, sqrt_n + 1, 2):
        if n % i == 0:
            return False
    return True


def check_primes():
    with concurrent.futures.ProcessPoolExecutor() as executor:
        start = time.time()

        for number, prime in zip(PRIMES, executor.map(is_prime, PRIMES)):
            print('%d is prime: %s' % (number, prime))

        print(f'processing took {time.time() - start} seconds')


async def main() -> None:
    # thin shim: the pool blocks this (sole) trio task
    # which is just fine for a one-shot baseline script.
    check_primes()


if __name__ == '__main__':

    start = time.time()
    trio.run(main)
    print(f'script took {time.time() - start} seconds')
