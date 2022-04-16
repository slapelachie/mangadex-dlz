"""The main handler for mangadex-dl"""
import os
import sys
import shutil
import json
import logging
from typing import Dict

from requests import RequestException
from PIL.Image import Image

import mangadex_dl
from mangadex_dl import series as md_series
from mangadex_dl import chapter as md_chapter

logger = logging.getLogger(__name__)


class FailedImageError(Exception):
    """Raised when image fails to download or be processed"""


class ComicInfoError(Exception):
    """Raised when ComicInfo.xml fails to be created"""


class MangaDexDL:
    """Handles all MangaDexDL related stuff"""

    def __init__(
        self,
        cache_file_path: str,
        out_directory: str,
        override: bool = False,
        download_cover: bool = False,
        download_chapter_cover: bool = False,
    ):
        """
        Arguments:
            cache_file_path (str): the path for the file containing downloaded hashes is stored
            out_directory (str): where to store the downloaded content
        """
        self._cache_file_path = cache_file_path
        self._output_directory = out_directory
        self._override = override
        self._download_cover = download_cover
        self._download_chapter_cover = download_chapter_cover

        try:
            self._ensure_cache_file_exists()
        except OSError as err:
            logger.exception(err)
            sys.exit(1)

    def _handle_mangadex_url(self, url: str):
        try:
            mangadex_type, resource_id = mangadex_dl.get_mangadex_resource(url)
        except ValueError as err:
            raise ValueError from err

        logger.debug("Supplied URL is of type %s", mangadex_type)
        logger.debug("Resource ID is %s", resource_id)

        if mangadex_type == "title":
            self.handle_series_id(resource_id)
        elif mangadex_type == "chapter":
            self.handle_chapter_id(resource_id)

    def _ensure_cache_file_exists(self):
        if not os.path.exists(self._cache_file_path):
            logger.info("%s does not exist, creating it...", self._cache_file_path)
            os.makedirs(os.path.dirname(self._cache_file_path), exist_ok=True)
            try:
                with open(self._cache_file_path, "w+", encoding="utf-8") as fout:
                    json.dump({}, fout, indent=4)
            except OSError as err:
                raise OSError(
                    f"Could not create required cache file at {self._cache_file_path}"
                ) from err

    def _add_chapter_to_downloaded(self, series_id: str, chapter_id: str):
        for _ in range(2):
            try:
                with open(self._cache_file_path, "r+", encoding="utf-8") as raw_file:
                    file_data = json.load(raw_file)
                    file_data.setdefault(series_id, []).append(chapter_id)
                    raw_file.seek(0)
                    json.dump(file_data, raw_file, indent=4)
                    break
            except FileNotFoundError:
                try:
                    self._ensure_cache_file_exists()
                except OSError:
                    continue
        else:
            raise FileNotFoundError(
                f"Could not find required cache file at {self._cache_file_path}"
            )

    def _process_chapter(
        self,
        chapter_info: mangadex_dl.ChapterInfo,
        series_info: mangadex_dl.SeriesInfo,
        volume_images: Dict[str, Image] = None,
    ):
        if "title" not in series_info or not all(
            key in chapter_info for key in ["chapter", "title", "id"]
        ):
            raise KeyError(
                "Needed information from chapter or series not present! Exiting..."
            )

        if volume_images is None:
            volume_images = {}

        series_title = series_info.get("title")
        chapter_number = chapter_info.get("chapter")
        chapter_title = chapter_info.get("title")

        logger.info(
            'Processing "%s" chapter "%s %s"',
            series_title,
            chapter_number,
            chapter_title,
        )

        chapter_directory = os.path.join(
            self._output_directory,
            md_chapter.get_chapter_directory(
                series_title, float(chapter_number), chapter_title
            ),
        )

        try:
            md_chapter.download_chapter(chapter_directory, chapter_info, series_info)
        except (KeyError, OSError) as err:
            raise FailedImageError("Failed to download image!") from err

        if not self._override:
            try:
                self._add_chapter_to_downloaded(
                    series_info.get("id"), chapter_info.get("id")
                )
            except OSError as err:
                raise OSError from err

        logger.info(
            "Creating %s.cbz",
            chapter_directory,
        )

        try:
            mangadex_dl.create_comicinfo(chapter_directory, chapter_info, series_info)
        except (KeyError, OSError) as err:
            shutil.rmtree(chapter_directory)
            raise ComicInfoError("Failed to create ComicInfo.xml!") from err

        try:
            mangadex_dl.create_cbz(chapter_directory)
        except (NotADirectoryError, OSError) as err:
            shutil.rmtree(chapter_directory)
            raise OSError("Failed to create archive!") from err

        shutil.rmtree(chapter_directory)

        volume_number = str(chapter_info.get("volume") or "")
        if volume_number != "":
            volume_image = volume_images.get(volume_number)
            if volume_image is not None:
                volume_image.save(f"{chapter_directory}.jpg")

    def _get_excluded_chapters_from_cache(self):
        excluded_chapters = []

        if self._override:
            return []

        chapter_cache = md_chapter.get_chapter_cache(self._cache_file_path)
        for series in chapter_cache:
            excluded_chapters.extend(chapter_cache[series])

        return excluded_chapters

    def handle_url(self, url: str):
        """
        Handle the given url

        Arguments:
            url (str): the url to handle
        """
        if mangadex_dl.is_mangadex_url(url):
            self._handle_mangadex_url(url)
        else:
            logger.critical("Not a valid MangaDex URL")
            sys.exit(1)

    def handle_series_id(self, series_id: str):
        """
        Handles a given series ID and starts the download process of the entire series

        Arguments:
            series_id (str): the UUID of the mangadex series
        """
        volume_images = {}
        logger.info("Handling series with ID: %s", series_id)

        try:
            series_info = md_series.get_series_info(series_id)
        except (ValueError, RequestException) as err:
            logger.exception(err)
            sys.exit(1)

        logger.info(
            "Got series information for %s (%s)", series_info.get("title"), series_id
        )

        if self._download_cover:
            logger.info("Downloading cover for %s...", series_info.get("title"))
            try:
                md_series.download_cover(series_info, self._output_directory)
            except (KeyError, OSError) as err:
                logger.exception(err)
            else:
                logger.info("Downloaded cover for %s", series_info.get("title"))

        excluded_chapters = self._get_excluded_chapters_from_cache()
        logger.info("Getting chapters for %s (%s)", series_info.get("title"), series_id)
        series_chapters = md_series.get_series_chapters(
            series_id, excluded_chapters=excluded_chapters
        )

        if self._download_chapter_cover:
            logger.info("Getting volume images for %s", series_info.get("title"))
            volume_images = md_series.get_needed_volume_images(
                series_id, series_chapters
            )

        # Process all chapters
        for chapter in series_chapters:
            try:
                self._process_chapter(chapter, series_info, volume_images=volume_images)
            except (KeyError, FailedImageError, OSError, ComicInfoError) as err:
                logger.exception(err)
                sys.exit(1)

    def handle_chapter_id(self, chapter_id: str):
        """
        Handles a given chapter id and prepares to start downloading the chapter

        Arguments:
            chapter_id (str): the UUID of the mangadex chapter
        """
        logger.info("Handling chapter with ID: %s", chapter_id)

        excluded_chapters = self._get_excluded_chapters_from_cache()
        if chapter_id not in excluded_chapters:
            try:
                chapter_info = md_chapter.get_chapter_info(chapter_id)
                series_info = md_series.get_series_info(chapter_info.get("series_id"))
            except (RequestException, ValueError, KeyError) as err:
                logger.exception(err)
                sys.exit(1)

            if self._download_chapter_cover:
                logger.info("Getting volume images for %s", series_info.get("title"))
                volume_images = md_series.get_needed_volume_images(
                    series_info.get("id"), [chapter_info]
                )

            try:
                self._process_chapter(
                    chapter_info, series_info, volume_images=volume_images
                )
            except (KeyError, FailedImageError, OSError, ComicInfoError) as err:
                logger.exception(err)
                sys.exit(1)
