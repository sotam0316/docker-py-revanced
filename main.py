import re
import subprocess
import sys
from atexit import register
from pathlib import Path
from queue import PriorityQueue
from shutil import rmtree
from subprocess import PIPE, Popen
from time import perf_counter
from typing import Any, Dict, List, Tuple

from environs import Env
from loguru import logger
from requests import Session
from selectolax.lexbor import LexborHTMLParser
from tqdm import tqdm

env = Env()
temp_folder = Path("apks")
session = Session()
session.headers["User-Agent"] = "anything"
supported_apps = [
    "youtube",
    "youtube_music",
    "twitter",
    "reddit",
    "tiktok",
    "warnwetter",
]
apps = env.list("PATCH_APPS", supported_apps)
apk_mirror = "https://www.apkmirror.com"
github = "https://www.github.com"
apk_mirror_urls = {
    "reddit": f"{apk_mirror}/apk/redditinc/reddit/",
    "twitter": f"{apk_mirror}/apk/twitter-inc/twitter/",
    "tiktok": f"{apk_mirror}/apk/tiktok-pte-ltd/tik-tok-including-musical-ly/",
    "warnwetter": f"{apk_mirror}/apk/deutscher-wetterdienst/warnwetter/",
    "youtube": f"{apk_mirror}/apk/google-inc/youtube/",
    "youtube_music": f"{apk_mirror}/apk/google-inc/youtube-music/",
}
apk_mirror_version_urls = {
    "reddit": f"{apk_mirror_urls.get('reddit')}reddit",
    "twitter": f"{apk_mirror_urls.get('twitter')}twitter",
    "tiktok": f"{apk_mirror_urls.get('tiktok')}tik-tok-including-musical-ly",
    "warnwetter": f"{apk_mirror_urls.get('warnwetter')}warnwetter",
    "youtube": f"{apk_mirror_urls.get('youtube')}youtube",
    "youtube_music": f"{apk_mirror_urls.get('youtube_music')}youtube-music",
}


class Downloader:
    _CHUNK_SIZE = 2**21 * 5
    _QUEUE = PriorityQueue()
    _QUEUE_LENGTH = 0

    @classmethod
    def _download(cls, url: str, file_name: str) -> None:
        logger.debug(f"Trying to download {file_name} from {url}")
        cls._QUEUE_LENGTH += 1
        start = perf_counter()
        resp = session.get(url, stream=True)
        total = int(resp.headers.get("content-length", 0))
        bar = tqdm(
            desc=file_name,
            total=total,
            unit="iB",
            unit_scale=True,
            unit_divisor=1024,
            colour="green",
        )
        with temp_folder.joinpath(file_name).open("wb") as dl_file, bar:
            for chunk in resp.iter_content(cls._CHUNK_SIZE):
                size = dl_file.write(chunk)
                bar.update(size)
        cls._QUEUE.put((perf_counter() - start, file_name))
        logger.debug(f"Downloaded {file_name}")

    @classmethod
    def extract_download_link(cls, page: str, app: str):
        logger.debug(f"Extracting download link from\n{page}")
        parser = LexborHTMLParser(session.get(page).text)

        resp = session.get(
            apk_mirror + parser.css_first("a.accent_bg").attributes["href"]
        )
        parser = LexborHTMLParser(resp.text)

        href = parser.css_first(
            "p.notes:nth-child(3) > span:nth-child(1) > a:nth-child(1)"
        ).attributes["href"]
        cls._download(apk_mirror + href, f"{app}.apk")
        logger.debug("Finished Extracting link and downloading")

    @classmethod
    def get_download_page(cls, parser, main_page):
        apm = parser.css(".apkm-badge")
        sub_url = ""
        for is_apm in apm:
            if "APK" in is_apm.text():
                parser = is_apm.parent
                sub_url = parser.css_first(".accent_color").attributes["href"]
                break
        if sub_url == "":
            logger.exception(
                f"Unable to find any apk on apkmirror_specific_version on {main_page}"
            )
            sys.exit(-1)
        download_url = apk_mirror + sub_url
        return download_url

    @classmethod
    def apkmirror_specific_version(cls, app: str, version: str) -> None:
        logger.debug(f"Trying to download {app},specific version {version}")
        version = version.replace(".", "-")
        main_page = f"{apk_mirror_version_urls.get(app)}-{version}-release/"
        parser = LexborHTMLParser(session.get(main_page).text)
        download_page = cls.get_download_page(parser, main_page)
        cls.extract_download_link(download_page, app)
        logger.debug(f"Downloaded {app} apk from apkmirror_specific_version")

    @classmethod
    def apkmirror_latest_version(cls, app: str) -> str:
        logger.debug(f"Trying to download {app}'s latest version from apkmirror")
        page = apk_mirror_urls.get(app)
        if not page:
            logger.debug("Invalid app")
            sys.exit(1)
        parser = LexborHTMLParser(session.get(page).text)
        main_page = parser.css_first(".appRowVariantTag>.accent_color").attributes[
            "href"
        ]
        int_version = re.search(r"\d", main_page).start()
        extra_release = main_page.rfind("release") - 1
        version = main_page[int_version:extra_release]
        version = version.replace("-", ".")
        main_page = f"{apk_mirror}{main_page}"
        parser = LexborHTMLParser(session.get(main_page).text)
        download_page = cls.get_download_page(parser, main_page)
        cls.extract_download_link(download_page, app)
        logger.debug(f"Downloaded {app} apk from apkmirror_specific_version in rt")
        return version

    @classmethod
    def repository(
        cls,
        name: str,
        owner: str = "revanced",
    ) -> None:
        logger.debug(f"Trying to download {name} from github")
        resp = session.get(f"{github}/{owner}/{name}/releases/latest")
        parser = LexborHTMLParser(resp.text)
        url = parser.css("li.Box-row > div:nth-child(1) > a:nth-child(2)")[:-2][
            -1
        ].attributes["href"]
        extension = url.rfind(".")
        cls._download(github + url, name + url[extension:])

    @classmethod
    def report(cls) -> None:
        started = False
        while True:
            item = cls._QUEUE.get()
            logger.debug(f"{item[1]} downloaded in {item[0]:.2f} seconds.")
            cls._QUEUE.task_done()
            cls._QUEUE_LENGTH -= 1

            if not started:
                started = True
            elif started and not cls._QUEUE_LENGTH:
                break


class Patches:
    def __init__(self) -> None:
        logger.debug("fetching all patches")
        resp = session.get(
            "https://raw.githubusercontent.com/revanced/revanced-patches/main/README.md"
        )
        available_patches = []
        for app in resp.text.split("### 📦 ")[1:]:
            lines = app.splitlines()

            app_name = lines[0][1:-1]
            app_patches = []
            for line in lines:
                patch = line.split("|")[1:-1]
                if len(patch) == 3:
                    (n, d, v), a = [i.replace("`", "").strip() for i in patch], app_name
                    app_patches.append((n, d, a, v))

            available_patches.extend(app_patches[2:])

        youtube, music, twitter, reddit, tiktok, warnwetter = [], [], [], [], [], []
        for n, d, a, v in available_patches:
            patch = {"name": n, "description": d, "app": a, "version": v}
            if "twitter" in a:
                twitter.append(patch)
            elif "reddit" in a:
                reddit.append(patch)
            elif "music" in a:
                music.append(patch)
            elif "youtube" in a:
                youtube.append(patch)
            elif "trill" in a:
                tiktok.append(patch)
            elif "warnwetter" in a:
                warnwetter.append(patch)
        self._yt = youtube
        self._ytm = music
        self._twitter = twitter
        self._reddit = reddit
        self._tiktok = tiktok
        self._warnwetter = warnwetter
        logger.debug(f"Total patches in youtube are {len(youtube)}")
        logger.debug(f"Total patches in youtube-music are {len(music)}")
        logger.debug(f"Total patches in twitter are {len(twitter)}")
        logger.debug(f"Total patches in reddit are {len(reddit)}")
        logger.debug(f"Total patches in tiktok are {len(tiktok)}")
        logger.debug(f"Total patches in warnwetter are {len(warnwetter)}")

    def get(self, app: str) -> Tuple[List[Dict[str, str]], str]:
        logger.debug("Getting patches for %s" % app)
        if "twitter" == app:
            patches = self._twitter
        elif "reddit" == app:
            patches = self._reddit
        elif "youtube_music" == app:
            patches = self._ytm
        elif "youtube" == app:
            patches = self._yt
        elif "tiktok" == app:
            patches = self._tiktok
        elif "warnwetter" == app:
            patches = self._warnwetter
        else:
            logger.debug("Invalid app name")
            sys.exit(-1)
        version = ""
        if app in ("youtube", "youtube_music"):
            version = next(i["version"] for i in patches if i["version"] != "all")
            logger.debug(f"Recommended Version for patching {app} is {version}")
        else:
            logger.debug("No recommended version.")
        return patches, version


class ArgParser:
    _PATCHES = []

    @classmethod
    def include(cls, name: str) -> None:
        cls._PATCHES.extend(["-i", name])

    @classmethod
    def exclude(cls, name: str) -> None:
        cls._PATCHES.extend(["-e", name])

    @classmethod
    def run(cls, app: str, version: str, is_experimental: bool = False) -> None:
        logger.debug(f"Sending request to revanced cli for building {app} revanced")
        args = [
            "-jar",
            "revanced-cli.jar",
            "-a",
            app + ".apk",
            "-b",
            "revanced-patches.jar",
            "-m",
            "revanced-integrations.apk",
            "-o",
            f"Re{app}-{version}-output.apk",
        ]
        if is_experimental:
            logger.debug("Using experimental features")
            args.append("--experimental")
        if app in ("reddit", "tiktok"):
            args.append("-r")
            args.remove("-m")
            args.remove("revanced-integrations.apk")

        args[1::2] = map(lambda i: temp_folder.joinpath(i), args[1::2])

        if cls._PATCHES:
            args.extend(cls._PATCHES)

        start = perf_counter()
        process = Popen(["java", *args], stdout=PIPE)
        for line in process.stdout:
            logger.debug(line.decode(), flush=True, end="")
        process.wait()
        logger.debug(
            f"Patching completed for app {app} in {perf_counter() - start:.2f} "
            f"seconds."
        )


@register
def close() -> None:
    session.close()
    cache = Path("revanced-cache")
    if cache.is_dir():
        rmtree(cache)


def check_java() -> None:
    logger.debug("Checking if java is available")
    jd = subprocess.check_output(["java", "-version"], stderr=subprocess.STDOUT)
    jd = str(jd)[1:-1]
    if "Runtime Environment" not in jd:
        logger.debug("Java Must be installed")
        exit(-1)
    if "17" not in jd:
        logger.debug("Java 17 Must be installed")
        exit(-1)
    logger.debug("Cool!! Java is available")


def pre_requisite():
    check_java()
    patches = Patches()
    return patches


def main() -> None:
    patches = pre_requisite()
    downloader = Downloader
    downloader.repository("revanced-cli")
    downloader.repository("revanced-integrations")
    downloader.repository("revanced-patches")
    downloader.repository("VancedMicroG", "TeamVanced")

    def get_patches() -> None:
        logger.debug(f"Excluding patches for app {app}")
        selected_patches = list(range(0, len(app_patches)))
        if app == "youtube":
            selected_patches.remove(9)
        for i, v in enumerate(app_patches):
            arg_parser.include(
                v["name"]
            ) if i in selected_patches else arg_parser.exclude(v["name"])
        logger.debug(f"Excluded patches for app {app}")

    def get_patches_version() -> Any:
        experiment = False
        total_patches, recommended_version = patches.get(app=app)
        env_version = env.str(f"{app}_VERSION".upper(), None)
        if env_version:
            logger.debug(f"Picked {app} version {env_version} from env.")
            if env_version == "latest" or env_version > recommended_version:
                experiment = True
            recommended_version = env_version
        return total_patches, recommended_version, experiment

    for app in apps:
        try:
            is_experimental = False
            arg_parser = ArgParser
            logger.debug("Trying to build %s" % app)
            app_patches, version, is_experimental = get_patches_version()
            if version and version != "latest":
                downloader.apkmirror_specific_version(app, version)
            else:
                version = downloader.apkmirror_latest_version(app)
            get_patches()
            logger.debug(f"Download completed {app}")
            arg_parser.run(app=app, version=version, is_experimental=is_experimental)
        except Exception as e:
            logger.exception(f"Failed to build {app} because of {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.error("Script halted because of keyboard interrupt.")
        sys.exit(-1)
