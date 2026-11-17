"""The download child process: `python -m tinybar_models.fetch REPO REVISION CACHE_DIR`.

A pull runs the Hugging Face download here, in its own process and session, so cancelling a pull can stop
it for certain (the parent terminates the process group). On success it prints the snapshot folder as its
last line. Files left half-fetched stay in the hub cache as `.incomplete` blobs, which the next download
resumes; nothing is marked downloaded until the parent has verified the folder.
"""

import sys


def main(argv: list[str] | None = None) -> int:
    repo, revision, cache_dir = (sys.argv[1:] if argv is None else argv)
    from huggingface_hub import snapshot_download

    print(snapshot_download(repo, revision=revision, cache_dir=cache_dir), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
