from typing import Any, Dict, List, Tuple
import os
import time
import threading

from app.chain.transfer import TransferChain
from app.core.event import eventmanager, Event
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from apscheduler.triggers.interval import IntervalTrigger


class FnLinkReverseDel(_PluginBase):
    plugin_name = "硬链接反向删除"
    plugin_desc = "监控硬链接目录，文件删除时同步删除关联种子"
    plugin_icon = "mediasyncdel.png"
    plugin_version = "1.5"
    plugin_author = "Samuel"
    author_url = "https://github.com/jxxghp/MoviePilot-Plugins"
    plugin_config_prefix = "fnlinkreversedel_"
    plugin_order = 50
    auth_level = 1

    _enabled = False
    _monitor_dirs = ""
    _path_mappings = ""
    _exclude_keywords = ""
    _temp_extensions = ['.mp', '.part', '.tmp', '.temp', '.!qB', '.!qb', '.downloading', '.crdownload']
    _delay_delete = 5
    _orphan_scan_interval = 3600
    _watch_thread = None
    _watch_running = False
    _processing_paths = set()
    _processing_lock = threading.Lock()
    _recent_processed = {}
    _transferchain = None
    _downloadhis = None

    def init_plugin(self, config: dict = None):
        self._transferchain = TransferChain()
        self._downloadhis = self._transferchain.downloadhis
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._monitor_dirs = config.get("monitor_dirs", "")
        self._path_mappings = config.get("path_mappings", "")
        self._exclude_keywords = config.get("exclude_keywords", "")
        self._delay_delete = int(config.get("delay_delete", 5))
        self._orphan_scan_interval = int(config.get("orphan_scan_interval", 3600))
        self._stop_watcher()
        if self._enabled:
            self._start_watcher()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/fnlink_scan",
                "event": EventType.PluginAction,
                "desc": "手动扫描孤儿种子",
                "category": "插件命令",
                "data": {"action": "fnlink_scan"},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "delay_delete", "label": "延迟删除(秒)", "type": "number", "min": 0, "max": 60},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {"component": "VTextarea", "props": {"model": "monitor_dirs", "label": "监控目录(硬链接目录)", "rows": 3, "placeholder": "多个目录用换行分隔，如：/video/link"}},
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {"component": "VTextarea", "props": {"model": "path_mappings", "label": "路径映射", "rows": 2, "placeholder": "硬链接路径 -> 下载源路径，如：/video/link -> /downloads"}},
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {"component": "VTextField", "props": {"model": "exclude_keywords", "label": "排除关键字", "placeholder": "逗号分隔"}},
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "orphan_scan_interval", "label": "孤儿扫描间隔(秒)", "type": "number", "min": 60, "max": 86400},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "warning",
                                            "variant": "tonal",
                                            "text": "⚠️ 注意：监控目录必须配置为硬链接目录（媒体库目录），不是下载源目录。路径映射用于将硬链接路径转换为下载器内的源文件路径。"
                                        }
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "monitor_dirs": "",
            "path_mappings": "",
            "exclude_keywords": "",
            "delay_delete": 5,
            "orphan_scan_interval": 3600,
        }

    def get_page(self) -> List[dict]:
        return [
            {
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "text": "硬链接反向删除插件 - 监控硬链接目录内文件被删除时同步删除关联做种种子"},
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self.get_state():
            return []
        services = []
        if self._orphan_scan_interval > 0:
            services.append({
                "id": "FnLinkReverseDel.OrphanScan",
                "name": "硬链接反向删除-孤儿扫描",
                "trigger": IntervalTrigger(seconds=self._orphan_scan_interval),
                "func": self.scan_orphan_torrents,
                "kwargs": {},
            })
        return services

    def stop_service(self):
        self._stop_watcher()

    def _start_watcher(self):
        self._stop_watcher()
        monitor_dirs = [d.strip() for d in self._monitor_dirs.split("\n") if d.strip()]
        if not monitor_dirs:
            logger.warning("[硬链接反向删除] 未配置监控目录")
            return
        for d in monitor_dirs:
            if not os.path.isdir(d):
                logger.warning(f"[硬链接反向删除] 监控目录不存在: {d}")
        self._watch_running = True
        self._watch_thread = threading.Thread(
            target=self._watch_loop,
            args=(monitor_dirs,),
            daemon=True,
            name="FnLinkReverseDel.Watcher"
        )
        self._watch_thread.start()
        logger.info(f"[硬链接反向删除] 目录监控已启动，监控目录: {monitor_dirs}")

    def _stop_watcher(self):
        self._watch_running = False
        if self._watch_thread and self._watch_thread.is_alive():
            self._watch_thread.join(timeout=5)
        self._watch_thread = None

    def _normalize_path(self, path_str: str) -> str:
        return path_str.replace('\\', '/').rstrip('/')

    def _is_temp_file(self, path_str: str) -> bool:
        path_lower = path_str.lower()
        for ext in self._temp_extensions:
            if path_lower.endswith(ext):
                return True
        return False

    def _should_exclude(self, path_str: str) -> bool:
        if self._is_temp_file(path_str):
            return True
        if self._exclude_keywords:
            keywords = [k.strip() for k in self._exclude_keywords.split(",") if k.strip()]
            for keyword in keywords:
                if keyword and keyword in path_str:
                    return True
        return False

    def _is_in_monitor_dirs(self, file_path: str) -> bool:
        monitor_dirs = [d.strip() for d in self._monitor_dirs.split("\n") if d.strip()]
        file_path_norm = self._normalize_path(os.path.normpath(file_path))
        for monitor_dir in monitor_dirs:
            monitor_dir_norm = self._normalize_path(os.path.normpath(monitor_dir))
            if file_path_norm == monitor_dir_norm:
                return True
            if file_path_norm.startswith(monitor_dir_norm + "/"):
                return True
        return False

    def _is_recently_processed(self, file_path: str, window: int = 10) -> bool:
        now = time.time()
        path_norm = self._normalize_path(file_path)
        with self._processing_lock:
            expired = [k for k, v in self._recent_processed.items() if now - v > window]
            for k in expired:
                del self._recent_processed[k]
            if path_norm in self._recent_processed:
                return True
            for processed_path in list(self._recent_processed.keys()):
                if path_norm.startswith(processed_path + "/") or processed_path.startswith(path_norm + "/"):
                    return True
            self._recent_processed[path_norm] = now
            return False

    def _watch_loop(self, monitor_dirs: List[str]):
        try:
            from watchfiles import watch, Change
            from watchfiles.filters import BaseFilter

            class _DeleteFilter(BaseFilter):
                def __init__(self, plugin):
                    self.plugin = plugin
                    super().__init__()

                def __call__(self, change: Change, path: str) -> bool:
                    if change != Change.deleted:
                        return False
                    path_str = str(path)
                    if self.plugin._should_exclude(path_str):
                        return False
                    if not self.plugin._is_in_monitor_dirs(path_str):
                        return False
                    return True

            normalized_dirs = []
            for d in monitor_dirs:
                normalized_dirs.append(self._normalize_path(os.path.normpath(d)))

            for changes in watch(*normalized_dirs, watch_filter=_DeleteFilter(self), force_polling=False):
                if not self._watch_running:
                    break
                for change_type, path in changes:
                    if change_type == Change.deleted:
                        path_str = str(path)
                        path_norm = self._normalize_path(path_str)
                        if self._is_recently_processed(path_norm):
                            logger.debug(f"[硬链接反向删除] 跳过最近处理过的路径: {path_norm}")
                            continue
                        logger.info(f"[硬链接反向删除] 检测到文件删除: {path_norm}")
                        threading.Thread(
                            target=self._async_handle_delete,
                            args=(path_norm,),
                            daemon=True
                        ).start()
        except ImportError:
            logger.warning("[硬链接反向删除] watchfiles未安装，将仅使用事件兜底监听")
        except Exception as e:
            logger.error(f"[硬链接反向删除] 目录监控异常: {str(e)}", exc_info=True)

    def _async_handle_delete(self, file_path: str):
        with self._processing_lock:
            if file_path in self._processing_paths:
                logger.debug(f"[硬链接反向删除] 文件正在处理中，跳过: {file_path}")
                return
            self._processing_paths.add(file_path)
        try:
            self.handle_file_delete(file_path)
        finally:
            with self._processing_lock:
                self._processing_paths.discard(file_path)

    @eventmanager.register(EventType.DownloadFileDeleted)
    def on_download_file_deleted(self, event: Event):
        if not self.get_state():
            return
        event_data = event.event_data or {}
        file_path = event_data.get("file_path") or event_data.get("src")
        if file_path:
            file_norm = self._normalize_path(file_path)
            logger.info(f"[硬链接反向删除] 系统删除事件触发: {file_norm}")
            threading.Thread(
                target=self._async_handle_delete,
                args=(file_norm,),
                daemon=True
            ).start()

    @eventmanager.register(EventType.PluginAction)
    def handle_plugin_action(self, event: Event):
        if not self.get_state():
            return
        event_data = event.event_data or {}
        if event_data.get("action") == "fnlink_scan":
            self.scan_orphan_torrents()

    def handle_file_delete(self, file_path: str):
        if not self.get_state():
            return
        file_path = self._normalize_path(file_path)
        if not self._is_in_monitor_dirs(file_path):
            logger.debug(f"[硬链接反向删除] 文件不在监控目录内，跳过: {file_path}")
            return
        if self._delay_delete > 0:
            for i in range(3):
                time.sleep(self._delay_delete / 3)
                if os.path.exists(file_path):
                    logger.info(f"[硬链接反向删除] 文件已恢复，跳过删除: {file_path}")
                    return
        if os.path.exists(file_path):
            logger.info(f"[硬链接反向删除] 文件仍存在，跳过删除: {file_path}")
            return
        self.handle_torrent(file_path)

    def _path_matches(self, tf_path: str, target_path: str) -> bool:
        if not tf_path or not target_path:
            return False
        tf_norm = self._normalize_path(os.path.normpath(tf_path))
        target_norm = self._normalize_path(os.path.normpath(target_path))
        if tf_norm == target_norm:
            return True
        if tf_norm.startswith(target_norm + "/"):
            return True
        tf_parent = self._normalize_path(os.path.dirname(tf_norm))
        if tf_parent == target_norm:
            return True
        return False

    def _map_path(self, file_path: str) -> str:
        file_path = self._normalize_path(file_path)
        if not self._path_mappings:
            return file_path
        for mapping in self._path_mappings.split("\n"):
            mapping = mapping.strip()
            if "->" in mapping:
                parts = mapping.split("->", 1)
                src = self._normalize_path(parts[0].strip())
                dst = self._normalize_path(parts[1].strip())
                if src and file_path.startswith(src):
                    return file_path.replace(src, dst, 1)
            if "：" in mapping:
                parts = mapping.split("：", 1)
                src = self._normalize_path(parts[0].strip())
                dst = self._normalize_path(parts[1].strip())
                if src and file_path.startswith(src):
                    return file_path.replace(src, dst, 1)
        return file_path

    def _get_all_downloaders(self):
        downloader_helper = DownloaderHelper()
        downloader_services = []
        try:
            services = downloader_helper.get_services()
            if services:
                for service in services:
                    if hasattr(service, 'instance'):
                        downloader_services.append(service)
        except Exception:
            pass
        if not downloader_services:
            try:
                service = downloader_helper.get_service()
                if service:
                    downloader_services.append(service)
            except Exception:
                pass
        return downloader_services

    def handle_torrent(self, file_path: str):
        logger.info(f"[硬链接反向删除] 处理文件删除: {file_path}")
        mapped_path = self._map_path(file_path)

        torrent_hash = None
        downloader_name = None
        try:
            if self._downloadhis:
                torrent_hash = self._downloadhis.get_hash_by_fullpath(mapped_path)
                if not torrent_hash and mapped_path != file_path:
                    torrent_hash = self._downloadhis.get_hash_by_fullpath(file_path)
                if torrent_hash:
                    download_files = self._downloadhis.get_files_by_hash(torrent_hash)
                    if download_files:
                        for df in download_files:
                            if df and hasattr(df, 'downloader') and df.downloader:
                                downloader_name = df.downloader
                                break
        except Exception as e:
            logger.debug(f"[硬链接反向删除] 从下载历史查询失败，将遍历种子列表: {str(e)}")

        if torrent_hash:
            self._handle_torrent_by_hash(torrent_hash, file_path, downloader_name)
        else:
            self._handle_torrent_by_scan(file_path, mapped_path)

    def _handle_torrent_by_hash(self, torrent_hash: str, file_path: str, downloader_name: str = None):
        try:
            download_files = self._downloadhis.get_files_by_hash(torrent_hash)
            if not download_files:
                logger.warning(f"[硬链接反向删除] 未找到种子 {torrent_hash} 的文件记录")
                self.chain.remove_torrents(hashs=torrent_hash, downloader=downloader_name)
                logger.info(f"[硬链接反向删除] 已删除种子: {torrent_hash}")
                return

            try:
                self._downloadhis.delete_file_by_fullpath(fullpath=self._map_path(file_path))
            except Exception:
                pass

            existing_link_count = 0
            for download_file in download_files:
                file_path_str = None
                if hasattr(download_file, 'path'):
                    file_path_str = download_file.path
                elif hasattr(download_file, 'fullpath'):
                    file_path_str = download_file.fullpath
                if not file_path_str:
                    continue
                source_norm = self._normalize_path(file_path_str)
                linked_path = self._reverse_map_path(source_norm)
                if self._normalize_path(file_path) == source_norm or (linked_path and self._normalize_path(file_path) == linked_path):
                    continue
                if linked_path and os.path.exists(linked_path):
                    existing_link_count += 1
                elif os.path.exists(source_norm):
                    existing_link_count += 1

            if existing_link_count > 0:
                logger.info(f"[硬链接反向删除] 种子 {torrent_hash} 还有 {existing_link_count} 个文件的硬链接存在，暂停种子")
                self.chain.stop_torrents(hashs=torrent_hash, downloader=downloader_name)
            else:
                logger.info(f"[硬链接反向删除] 种子 {torrent_hash} 所有硬链接已删除，删除种子及源文件")
                self.chain.remove_torrents(hashs=torrent_hash, downloader=downloader_name)
        except Exception as e:
            logger.error(f"[硬链接反向删除] 处理种子 {torrent_hash} 失败: {str(e)}", exc_info=True)

    def _reverse_map_path(self, source_path: str) -> str:
        if not self._path_mappings:
            return None
        source_path = self._normalize_path(source_path)
        for mapping in self._path_mappings.split("\n"):
            mapping = mapping.strip()
            if "->" in mapping:
                parts = mapping.split("->", 1)
                link_prefix = self._normalize_path(parts[0].strip())
                src_prefix = self._normalize_path(parts[1].strip())
                if src_prefix and source_path.startswith(src_prefix):
                    return source_path.replace(src_prefix, link_prefix, 1)
            if "：" in mapping:
                parts = mapping.split("：", 1)
                link_prefix = self._normalize_path(parts[0].strip())
                src_prefix = self._normalize_path(parts[1].strip())
                if src_prefix and source_path.startswith(src_prefix):
                    return source_path.replace(src_prefix, link_prefix, 1)
        return None

    def _handle_torrent_by_scan(self, file_path: str, mapped_path: str):
        downloader_services = self._get_all_downloaders()
        if not downloader_services:
            logger.warning("[硬链接反向删除] 未找到启用的下载器")
            return

        deleted_hashes = set()
        file_norm = self._normalize_path(file_path)
        for service in downloader_services:
            try:
                service_name = getattr(service, 'name', 'default')
                torrents = service.instance.get_torrents()
                for torrent in torrents:
                    torrent_hash = getattr(torrent, 'hash', '') or getattr(torrent, 'hashString', '')
                    if not torrent_hash or torrent_hash in deleted_hashes:
                        continue
                    torrent_name = getattr(torrent, 'name', str(torrent_hash))
                    torrent_files = getattr(torrent, 'files', []) or []
                    file_paths = []
                    for tf in torrent_files:
                        if hasattr(tf, 'path'):
                            file_paths.append(self._normalize_path(str(tf.path)))
                        elif hasattr(tf, 'name'):
                            file_paths.append(self._normalize_path(str(tf.name)))
                        else:
                            file_paths.append(self._normalize_path(str(tf)))
                    if not file_paths:
                        torrent_path = getattr(torrent, 'path', '') or getattr(torrent, 'save_path', '')
                        if torrent_path:
                            file_paths = [self._normalize_path(str(torrent_path))]
                    match_found = False
                    existing_link_count = 0
                    for tf_path in file_paths:
                        if not tf_path:
                            continue
                        linked_path = self._reverse_map_path(tf_path)
                        if (self._path_matches(tf_path, file_path) or
                                self._path_matches(tf_path, mapped_path) or
                                (linked_path and self._path_matches(linked_path, file_path))):
                            match_found = True
                        if tf_path == file_norm or (linked_path and linked_path == file_norm):
                            continue
                        if linked_path and os.path.exists(linked_path):
                            existing_link_count += 1
                        elif os.path.exists(tf_path):
                            existing_link_count += 1
                    if match_found:
                        deleted_hashes.add(torrent_hash)
                        try:
                            if existing_link_count > 0:
                                logger.info(f"[硬链接反向删除] 找到关联种子({service_name})，还有{existing_link_count}个文件的硬链接存在，暂停: {torrent_name}")
                                self.chain.stop_torrents(hashs=torrent_hash, downloader=service_name)
                            else:
                                logger.info(f"[硬链接反向删除] 找到关联种子({service_name})，所有硬链接已删除，删除: {torrent_name}")
                                self.chain.remove_torrents(hashs=torrent_hash, downloader=service_name)
                        except Exception as e:
                            logger.error(f"[硬链接反向删除] 操作种子失败: {torrent_name}, 错误: {str(e)}")
            except Exception as e:
                logger.error(f"[硬链接反向删除] 遍历下载器失败({service_name}): {str(e)}")

        if not deleted_hashes:
            logger.info(f"[硬链接反向删除] 未找到关联种子: {file_path}")

    def scan_orphan_torrents(self):
        if not self.get_state():
            return
        monitor_dirs = [d.strip() for d in self._monitor_dirs.split("\n") if d.strip()]
        if not monitor_dirs:
            return

        logger.info("[硬链接反向删除] 开始扫描孤儿种子")
        self._scan_orphans_by_torrent_list(monitor_dirs)

    def _check_torrent_all_links_deleted(self, file_paths: List[str], monitor_dirs: List[str]) -> Tuple[bool, bool]:
        all_links_missing = True
        has_monitored = False
        for tf_path in file_paths:
            if not tf_path:
                continue
            tf_norm = self._normalize_path(tf_path)
            linked_path = self._reverse_map_path(tf_norm)
            in_monitor = False
            for md in monitor_dirs:
                md_norm = self._normalize_path(md)
                if tf_norm.startswith(md_norm + "/") or (linked_path and linked_path.startswith(md_norm + "/")):
                    in_monitor = True
                    has_monitored = True
                    break
            if in_monitor:
                if linked_path and os.path.exists(linked_path):
                    all_links_missing = False
                    break
                elif os.path.exists(tf_norm) and not linked_path:
                    all_links_missing = False
                    break
        return all_links_missing, has_monitored

    def _scan_orphans_by_torrent_list(self, monitor_dirs: List[str]):
        downloader_services = self._get_all_downloaders()
        if not downloader_services:
            logger.warning("[硬链接反向删除] 未找到启用的下载器")
            return

        deleted_count = 0
        paused_count = 0
        for service in downloader_services:
            try:
                service_name = getattr(service, 'name', 'default')
                torrents = service.instance.get_torrents()
                for torrent in torrents:
                    torrent_hash = getattr(torrent, 'hash', '') or getattr(torrent, 'hashString', '')
                    if not torrent_hash:
                        continue
                    torrent_name = getattr(torrent, 'name', str(torrent_hash))
                    torrent_files = getattr(torrent, 'files', []) or []
                    file_paths = []
                    for tf in torrent_files:
                        if hasattr(tf, 'path'):
                            file_paths.append(self._normalize_path(str(tf.path)))
                        elif hasattr(tf, 'name'):
                            file_paths.append(self._normalize_path(str(tf.name)))
                        else:
                            file_paths.append(self._normalize_path(str(tf)))
                    if not file_paths:
                        torrent_path = getattr(torrent, 'path', '') or getattr(torrent, 'save_path', '')
                        if torrent_path:
                            file_paths = [self._normalize_path(str(torrent_path))]
                    if not file_paths:
                        continue

                    all_missing, has_monitored = self._check_torrent_all_links_deleted(file_paths, monitor_dirs)
                    if not has_monitored:
                        continue

                    existing_link_count = 0
                    for tf_path in file_paths:
                        if not tf_path:
                            continue
                        linked_path = self._reverse_map_path(tf_path)
                        if linked_path and os.path.exists(linked_path):
                            existing_link_count += 1
                        elif os.path.exists(tf_path) and not linked_path:
                            existing_link_count += 1

                    try:
                        if all_missing or existing_link_count == 0:
                            self.chain.remove_torrents(hashs=torrent_hash, downloader=service_name)
                            deleted_count += 1
                            logger.info(f"[硬链接反向删除] 删除孤儿种子({service_name}): {torrent_name}")
                        elif existing_link_count < len(file_paths):
                            self.chain.stop_torrents(hashs=torrent_hash, downloader=service_name)
                            paused_count += 1
                            logger.info(f"[硬链接反向删除] 部分硬链接已删除，暂停种子({service_name}): {torrent_name}")
                    except Exception as e:
                        logger.error(f"[硬链接反向删除] 处理孤儿种子失败: {torrent_name}, 错误: {str(e)}")
            except Exception as e:
                logger.error(f"[硬链接反向删除] 孤儿扫描失败({service_name}): {str(e)}")

        logger.info(f"[硬链接反向删除] 孤儿种子扫描完成，删除 {deleted_count} 个，暂停 {paused_count} 个")
