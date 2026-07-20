import os
import time
import threading
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

from apscheduler.triggers.interval import IntervalTrigger

from app import schemas
from app.chain.storage import StorageChain
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType


class FnLinkReverseDel(_PluginBase):
    plugin_name = "硬链接反向删除"
    plugin_desc = "监控硬链接目录，文件删除时同步删除关联种子"
    plugin_icon = "mediasyncdel.png"
    plugin_version = "4.3"
    plugin_author = "Samuel"
    author_url = "https://github.com/jxxghp/MoviePilot-Plugins"
    plugin_config_prefix = "fnlinkreversedel_"
    plugin_order = 50
    auth_level = 1

    _enabled = False
    _monitor_dirs = ""
    _path_mappings = ""
    _exclude_keywords = ""
    _delay_delete = 5
    _orphan_scan_interval = 3600
    _force_polling = False
    _watch_thread = None
    _watch_running = False
    _scheduler = None
    _transferhis = None
    _downloadhis = None
    _downloader_helper = None
    _storagechain = None

    @staticmethod
    def _safe_int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def init_plugin(self, config: dict = None):
        self.stop_service()
        # 可变状态属性在实例上初始化，避免类级别共享
        self._processing_paths = set()
        self._processing_lock = threading.Lock()
        self._recent_processed = {}
        self._transferhis = TransferHistoryOper()
        self._downloadhis = DownloadHistoryOper()
        self._downloader_helper = DownloaderHelper()
        self._storagechain = StorageChain()

        if config:
            self._enabled = bool(config.get("enabled"))
            self._monitor_dirs = config.get("monitor_dirs") or ""
            self._path_mappings = config.get("path_mappings") or ""
            self._exclude_keywords = config.get("exclude_keywords") or ""
            self._delay_delete = self._safe_int(config.get("delay_delete"), 5)
            self._orphan_scan_interval = self._safe_int(config.get("orphan_scan_interval"), 3600)
            self._force_polling = bool(config.get("force_polling"))
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
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 8},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'delay_delete',
                                            'label': '延迟删除(秒)',
                                            'type': 'number',
                                            'placeholder': '5'
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'monitor_dirs',
                                            'label': '监控目录(硬链接目录/媒体库目录)',
                                            'rows': '3',
                                            'placeholder': '多个目录用换行分隔，如：/video/link'
                                        }
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'path_mappings',
                                            'label': '路径映射(媒体服务器路径:MoviePilot路径)',
                                            'rows': '2',
                                            'placeholder': '硬链接路径 -> 下载源路径，如：/video/link -> /downloads\n若媒体服务器路径与MoviePilot路径不同，用冒号分隔，如：/data/link:/video/link'
                                        }
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'exclude_keywords',
                                            'label': '排除关键字',
                                            'placeholder': '逗号分隔'
                                        }
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 8},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'orphan_scan_interval',
                                            'label': '孤儿扫描间隔(秒)',
                                            'type': 'number',
                                            'placeholder': '3600'
                                        },
                                    }
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'force_polling', 'label': '强制轮询(SMB/NFS)'}}
                                ],
                            },
                        ],
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '注意：监控目录必须配置为硬链接目录（媒体库目录），不是下载源目录。路径映射用于将硬链接路径转换为下载器内的源文件路径，路径一致可不填。参考mediasyncdel实现，依赖MoviePilot转移历史记录匹配种子，刮削重命名也能正确识别。'
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
            "force_polling": False,
        }

    def get_page(self) -> List[dict]:
        logs = self.get_data('logs') or []
        if not logs:
            return [
                {
                    'component': 'VAlert',
                    'props': {
                        'type': 'info',
                        'variant': 'tonal',
                        'text': '硬链接反向删除插件 - 监控硬链接目录内文件被删除时同步删除关联做种种子。暂无操作记录。'
                    }
                }
            ]
        logs = sorted(logs, key=lambda x: x.get('time', ''), reverse=True)[:20]
        contents = []
        for log_item in logs:
            contents.append({
                'component': 'div',
                'props': {'class': 'd-flex align-center pa-2 border-b'},
                'content': [
                    {
                        'component': 'div',
                        'props': {'class': 'text-caption text-grey mr-3 flex-no-shrink'},
                        'text': log_item.get('time', '')
                    },
                    {
                        'component': 'div',
                        'props': {'class': 'text-body-2 text-truncate'},
                        'text': log_item.get('message', '')
                    }
                ]
            })
        return [
            {
                'component': 'VAlert',
                'props': {
                    'type': 'info',
                    'variant': 'tonal',
                    'text': '硬链接反向删除插件 - 最近20条操作记录'
                }
            },
            {
                'component': 'div',
                'props': {'class': 'mt-3'},
                'content': contents
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
        if self._scheduler:
            try:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
            except Exception:
                pass
            self._scheduler = None

    def _log_action(self, message: str):
        try:
            logs = self.get_data('logs') or []
            logs.append({
                'time': time.strftime('%Y-%m-%d %H:%M:%S'),
                'message': message
            })
            logs = logs[-100:]
            self.save_data('logs', logs)
        except Exception:
            pass

    def _parse_monitor_dirs(self) -> List[str]:
        """解析监控目录，自动处理误填的路径映射格式"""
        monitor_dirs = []
        auto_mappings = []
        for line in self._monitor_dirs.split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = None
            for sep in ['->', '：', ':']:
                if sep in line:
                    candidate = line.split(sep, 1)
                    if len(candidate) == 2 and candidate[0].strip().startswith('/'):
                        parts = candidate
                        break
            if parts and len(parts) == 2:
                monitor_dir = parts[0].strip()
                src_dir = parts[1].strip()
                monitor_dirs.append(monitor_dir)
                if monitor_dir != src_dir:
                    auto_mappings.append(f"{monitor_dir}->{src_dir}")
            else:
                monitor_dirs.append(line)
        if auto_mappings and not self._path_mappings:
            self._path_mappings = "\n".join(auto_mappings)
            logger.info(f"[硬链接反向删除] 自动从监控目录提取路径映射: {auto_mappings}")
        return monitor_dirs

    def _start_watcher(self):
        self._stop_watcher()
        monitor_dirs = self._parse_monitor_dirs()
        if not monitor_dirs:
            logger.warning("[硬链接反向删除] 未配置监控目录")
            return
        valid_dirs = []
        for d in monitor_dirs:
            if not os.path.isdir(d):
                logger.warning(f"[硬链接反向删除] 监控目录不存在: {d}")
            else:
                valid_dirs.append(d)
        if not valid_dirs:
            logger.error("[硬链接反向删除] 没有有效的监控目录，监控未启动")
            return
        self._watch_running = True
        self._watch_thread = threading.Thread(
            target=self._watch_loop,
            args=(valid_dirs,),
            daemon=True,
            name="FnLinkReverseDel.Watcher"
        )
        self._watch_thread.start()
        logger.info(f"[硬链接反向删除] 目录监控已启动，监控目录: {valid_dirs}")

    def _stop_watcher(self):
        self._watch_running = False
        if self._watch_thread and self._watch_thread.is_alive():
            self._watch_thread.join(timeout=5)
        self._watch_thread = None

    @staticmethod
    def _normalize_path(path_str: str) -> str:
        return str(path_str).replace('\\', '/').rstrip('/')

    @classmethod
    def _parent_dir(cls, path_str: str) -> str:
        norm = cls._normalize_path(path_str)
        idx = norm.rfind('/')
        if idx <= 0:
            return norm
        return norm[:idx]

    def _is_media_file(self, path_str: str) -> bool:
        return os.path.splitext(str(path_str).lower())[1] in settings.RMT_MEDIAEXT

    def _is_temp_file(self, path_str: str) -> bool:
        temp_extensions = ['.mp', '.part', '.tmp', '.temp', '.!qB', '.!qb', '.downloading', '.crdownload']
        path_lower = path_str.lower()
        for ext in temp_extensions:
            if path_lower.endswith(ext):
                return True
        return False

    def _should_exclude(self, path_str: str) -> bool:
        if self._is_temp_file(path_str):
            return True
        if not self._is_media_file(path_str):
            return True
        if self._exclude_keywords:
            keywords = [k.strip() for k in self._exclude_keywords.split(",") if k.strip()]
            for keyword in keywords:
                if keyword and keyword in path_str:
                    return True
        return False

    def _is_in_monitor_dirs(self, file_path: str) -> bool:
        monitor_dirs = self._parse_monitor_dirs()
        file_path_norm = self._normalize_path(os.path.normpath(file_path))
        for monitor_dir in monitor_dirs:
            monitor_dir_norm = self._normalize_path(os.path.normpath(monitor_dir))
            if file_path_norm == monitor_dir_norm:
                return True
            if file_path_norm.startswith(monitor_dir_norm + "/"):
                return True
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

            for changes in watch(*normalized_dirs, watch_filter=_DeleteFilter(self), force_polling=self._force_polling):
                if not self._watch_running:
                    break
                for change_type, path in changes:
                    if change_type == Change.deleted:
                        path_str = str(path)
                        path_norm = self._normalize_path(path_str)
                        logger.info(f"[硬链接反向删除] 检测到文件删除: {path_norm}")
                        threading.Thread(
                            target=self._async_handle_delete,
                            args=(path_norm,),
                            daemon=True
                        ).start()
        except ImportError:
            logger.warning("[硬链接反向删除] watchfiles未安装，将仅使用系统事件兜底监听")
        except Exception as e:
            logger.error(f"[硬链接反向删除] 目录监控异常: {str(e)}", exc_info=True)

    def _async_handle_delete(self, file_path: str):
        with self._processing_lock:
            if file_path in self._processing_paths:
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
            if not self._is_media_file(file_norm):
                return
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

    def _map_path(self, path_str: str, direction: str = "to_src") -> str:
        path_norm = self._normalize_path(path_str)
        if not self._path_mappings:
            return path_norm
        for mapping in self._path_mappings.split("\n"):
            mapping = mapping.strip()
            if not mapping:
                continue
            if "->" in mapping:
                parts = mapping.split("->", 1)
            elif "：" in mapping:
                parts = mapping.split("：", 1)
            elif ":" in mapping and "/" in mapping.split(":", 1)[0]:
                parts = mapping.split(":", 1)
            else:
                continue
            if len(parts) < 2:
                continue
            left = self._normalize_path(parts[0].strip())
            right = self._normalize_path(parts[1].strip())
            if direction == "to_src":
                # 监控路径 → 源文件路径：left=监控目录, right=下载源目录
                if left and path_norm.startswith(left):
                    return path_norm.replace(left, right, 1)
            elif direction == "to_mp":
                # 源文件路径 → 监控路径：right=下载源目录, left=监控目录
                if right and path_norm.startswith(right):
                    return path_norm.replace(right, left, 1)
        return path_norm

    def handle_file_delete(self, file_path: str):
        if not self.get_state():
            return
        file_path = self._normalize_path(file_path)
        if not self._is_in_monitor_dirs(file_path):
            return
        if not self._is_media_file(file_path):
            return
        if self._delay_delete > 0:
            for _ in range(3):
                time.sleep(self._delay_delete / 3)
                if os.path.exists(file_path):
                    logger.info(f"[硬链接反向删除] 文件已恢复(重命名/移动)，跳过删除: {file_path}")
                    return
        if os.path.exists(file_path):
            return
        self._process_deleted_file(file_path)

    def _process_deleted_file(self, file_path: str):
        # 去重检查（在延迟等待之后、实际处理之前）
        with self._processing_lock:
            now = time.time()
            expired = [k for k, v in self._recent_processed.items() if now - v > 120]
            for k in expired:
                del self._recent_processed[k]
            if file_path in self._recent_processed:
                logger.info(f"[硬链接反向删除] 重复处理，已跳过: {file_path}")
                return
            self._recent_processed[file_path] = now

        logger.info(f"[硬链接反向删除] 处理文件删除: {file_path}")
        self._log_action(f"处理文件删除: {os.path.basename(file_path)}")

        src_path = None
        torrent_hash = None
        downloader_name = None
        # 收集所有需要删除的转移记录（步骤1和步骤2可能各查到一条）
        transfer_records_to_delete = []

        # 1. 通过转移历史用dest路径查询src源文件路径和hash
        # 注意：TransferHistoryOper 没有 get_by_dest 方法，使用 get_by(dest=...) 返回列表
        transferhis = None
        try:
            transferhis_list = self._transferhis.get_by(dest=file_path)
            if transferhis_list:
                # get_by 返回列表，取第一条（同一路径通常只有一条转移记录）
                transferhis = transferhis_list[0] if isinstance(transferhis_list, list) else transferhis_list
                src_path = getattr(transferhis, 'src', '') or (transferhis.get('src', '') if isinstance(transferhis, dict) else '') or ''
                download_hash = getattr(transferhis, 'download_hash', None) or (transferhis.get('download_hash') if isinstance(transferhis, dict) else None)
                if download_hash:
                    torrent_hash = str(download_hash)
                    logger.info(f"[硬链接反向删除] 转移记录找到hash: {torrent_hash}")
                else:
                    logger.info(f"[硬链接反向删除] 转移记录找到但hash为空, src: {src_path}")
                # 将所有匹配的转移记录都加入删除列表（通常只有1条，但保险起见全部加入）
                if isinstance(transferhis_list, list):
                    transfer_records_to_delete.extend(transferhis_list)
                else:
                    transfer_records_to_delete.append(transferhis)
            else:
                logger.info(f"[硬链接反向删除] get_by(dest)未找到记录: {file_path}")
        except Exception as e:
            logger.error(f"[硬链接反向删除] get_by(dest)查询失败: {str(e)}", exc_info=True)

        # 2. 尝试用父目录查询（不覆盖步骤1的transferhis，只获取src_path和hash）
        if not src_path:
            parent = self._parent_dir(file_path)
            try:
                parent_list = self._transferhis.get_by(dest=parent)
                if parent_list:
                    parent_transferhis = parent_list[0] if isinstance(parent_list, list) else parent_list
                    src_path = getattr(parent_transferhis, 'src', '') or (parent_transferhis.get('src', '') if isinstance(parent_transferhis, dict) else '') or ''
                    parent_hash = getattr(parent_transferhis, 'download_hash', None) or (parent_transferhis.get('download_hash') if isinstance(parent_transferhis, dict) else None)
                    if parent_hash and not torrent_hash:
                        torrent_hash = str(parent_hash)
                    logger.info(f"[硬链接反向删除] 通过父目录找到src: {src_path}")
                    # 父目录查到的转移记录也要加入删除列表
                    if isinstance(parent_list, list):
                        transfer_records_to_delete.extend(parent_list)
                    else:
                        transfer_records_to_delete.append(parent_transferhis)
            except Exception as e:
                logger.debug(f"[硬链接反向删除] 父目录查询失败: {str(e)}")

        # 3. 如果没有hash但有src路径，用src路径反查downloadhis
        if not torrent_hash and src_path:
            torrent_hash = self._find_hash_by_src(src_path)
            if torrent_hash:
                try:
                    dl = self._downloadhis.get_by_hash(torrent_hash)
                    if dl:
                        # 兼容字典和对象两种访问方式
                        downloader_name = getattr(dl, 'downloader', '') or (dl.get('downloader', '') if isinstance(dl, dict) else '') or ''
                except Exception:
                    pass

        # 4. 删除做种任务
        handled_hashes = set()
        if torrent_hash:
            logger.info(f"[硬链接反向删除] 处理种子: hash={torrent_hash}, downloader={downloader_name}")
            # 传入src_path用于标记下载历史，为空时传None避免用硬链接路径误标记
            self._handle_torrent(torrent_hash, src_path or None, downloader_name)
            handled_hashes.add(torrent_hash)

        # 5. 遍历所有下载器，用src源文件路径匹配并处理其他做种任务
        if src_path:
            logger.info(f"[硬链接反向删除] 遍历下载器处理其他做种任务: {src_path}")
            self._find_and_handle_torrent_by_file(src_path, exclude_hashes=handled_hashes)

        # 6. 删除转移记录和源文件（不管种子是暂停还是删除）
        if src_path:
            # 从转移记录获取src_fileitem（用于StorageChain删除）
            src_fileitem = None
            # 遍历所有查到的转移记录，逐个删除并提取src_fileitem
            for record in transfer_records_to_delete:
                try:
                    # 尝试从转移记录获取src_fileitem字段（包含完整存储信息）
                    if src_fileitem is None:
                        fileitem_dict = getattr(record, 'src_fileitem', None) or (record.get('src_fileitem') if isinstance(record, dict) else None)
                        if fileitem_dict and isinstance(fileitem_dict, dict):
                            src_fileitem = schemas.FileItem(**fileitem_dict)
                    his_id = getattr(record, 'id', None) or (record.get('id') if isinstance(record, dict) else None)
                    if his_id is not None:
                        self._transferhis.delete(his_id)
                        logger.info(f"[硬链接反向删除] 删除转移记录: {his_id}")
                        self._log_action(f"删除转移记录: {his_id}")
                except Exception as e:
                    logger.error(f"[硬链接反向删除] 删除转移记录失败: {str(e)}")
            self._delete_source_file(src_path, fileitem=src_fileitem)

    def _find_and_handle_torrent_by_file(self, src_path: str, exclude_hashes: set = None):
        """遍历所有下载器，通过源文件路径匹配做种任务并删除

        优化策略：
        1. 优先用 save_path+name 路径匹配（无API调用）
        2. 仅当方式1失败才调用 get_files 精确匹配
        3. 添加请求间隔避免触发 qBittorrent 限流（403 Forbidden）
        4. 连续失败熔断，避免无意义重试
        """
        src_norm = self._normalize_path(src_path)
        src_parent = self._parent_dir(src_norm)
        downloader_services = self._get_all_downloaders()
        if not downloader_services:
            logger.warning("[硬链接反向删除] 未找到启用的下载器")
            return
        for service_name, downloader in downloader_services:
            try:
                torrents = downloader.get_torrents()
                if not torrents:
                    continue
                if isinstance(torrents, tuple):
                    torrents = torrents[0]
                # 失败熔断：连续3次get_files返回None则停止该下载器遍历
                consecutive_failures = 0
                MAX_FAILURES = 3
                for torrent in torrents:
                    # 熔断检查
                    if consecutive_failures >= MAX_FAILURES:
                        logger.warning(f"[硬链接反向删除] 连续{MAX_FAILURES}次获取文件列表失败，停止遍历({service_name})，可能是qBittorrent会话过期或限流")
                        break
                    torrent_hash = ''
                    torrent_name = ''
                    if isinstance(torrent, dict):
                        torrent_hash = torrent.get('hash', '') or ''
                        torrent_name = torrent.get('name', str(torrent_hash))
                    else:
                        torrent_hash = getattr(torrent, 'hash', '') or ''
                        torrent_name = getattr(torrent, 'name', str(torrent_hash))
                    if not torrent_hash:
                        continue
                    # 跳过已通过hash路径处理过的种子
                    if exclude_hashes and torrent_hash in exclude_hashes:
                        continue
                    save_path = self._normalize_path(self._get_torrent_save_path(torrent))
                    matched = False
                    # 匹配方式1：种子的save_path + name 路径匹配（无API调用，优先使用）
                    torrent_content_path = f"{save_path}/{self._normalize_path(str(torrent_name))}"
                    if torrent_content_path == src_norm:
                        # 单文件种子：save_path/name == src
                        matched = True
                    elif torrent_content_path == src_parent:
                        # 多文件种子：save_path/name == src的父目录（src直接在种子根目录下）
                        matched = True
                    elif src_norm.startswith(torrent_content_path + "/"):
                        # 多文件种子嵌套：src在save_path/name/子目录下（如Season01/episode01.mkv）
                        matched = True
                    # 匹配方式2：遍历种子内文件列表精确匹配（仅在方式1失败时调用）
                    if not matched:
                        try:
                            # 添加请求间隔避免触发qBittorrent限流（403 Forbidden）
                            time.sleep(0.1)
                            torrent_files = downloader.get_files(torrent_hash)
                            if torrent_files is None:
                                # get_files返回None可能是会话过期或限流，计入失败计数
                                consecutive_failures += 1
                                logger.debug(f"[硬链接反向删除] get_files返回None({service_name}/{torrent_hash}), 连续失败{consecutive_failures}次")
                                continue
                            else:
                                # 成功获取则重置失败计数
                                consecutive_failures = 0
                            if torrent_files:
                                for tf in torrent_files:
                                    tf_name = ''
                                    if isinstance(tf, dict):
                                        tf_name = tf.get('name', '') or tf.get('path', '')
                                    elif hasattr(tf, 'name'):
                                        tf_name = str(tf.name)
                                    elif hasattr(tf, 'path'):
                                        tf_name = str(tf.path)
                                    else:
                                        tf_name = str(tf)
                                    if not tf_name:
                                        continue
                                    tf_full = self._normalize_path(f"{save_path}/{self._normalize_path(tf_name)}")
                                    if tf_full == src_norm:
                                        matched = True
                                        break
                        except Exception as e:
                            consecutive_failures += 1
                            logger.debug(f"[硬链接反向删除] 获取种子文件列表失败({service_name}/{torrent_hash}): {str(e)}, 连续失败{consecutive_failures}次")
                            continue
                    if matched:
                        logger.info(f"[硬链接反向删除] 下载器匹配到种子({service_name}): {torrent_name}")
                        # 复用_handle_torrent：基于downloadhis.state判断多文件场景
                        # mark_history=False 避免重复标记下载历史（步骤4已标记）
                        self._handle_torrent(torrent_hash, src_norm, service_name, mark_history=False)
            except Exception as e:
                logger.error(f"[硬链接反向删除] 遍历下载器失败({service_name}): {str(e)}")

    def _cleanup_orphan_related_records(self, torrent_hash: str, monitor_dirs: List[str]):
        """删除孤儿种子关联的转移记录和源文件（孤儿扫描专用）

        :param torrent_hash: 种子hash
        :param monitor_dirs: 监控目录列表，用于路径映射
        """
        try:
            dl_files = self._downloadhis.get_files_by_hash(torrent_hash)
            if not dl_files:
                return
            for df in dl_files:
                src_path = getattr(df, 'path', '') or getattr(df, 'fullpath', '') or ''
                if not src_path:
                    continue
                src_norm = self._normalize_path(str(src_path))
                if not self._is_media_file(src_norm):
                    continue
                # 将源文件路径映射为硬链接路径（用于查询转移记录）
                mp_path = self._map_path(src_norm, direction="to_mp")
                # 删除转移记录（通过dest硬链接路径查询，使用get_by(dest=...)返回列表）
                try:
                    transferhis_list = self._transferhis.get_by(dest=mp_path)
                    if not transferhis_list:
                        # 兜底：尝试用父目录查询
                        mp_parent = self._parent_dir(mp_path)
                        transferhis_list = self._transferhis.get_by(dest=mp_parent)
                    if transferhis_list:
                        # get_by 返回列表，遍历删除所有匹配的转移记录
                        records = transferhis_list if isinstance(transferhis_list, list) else [transferhis_list]
                        for record in records:
                            his_id = getattr(record, 'id', None) or (record.get('id') if isinstance(record, dict) else None)
                            if his_id is not None:
                                self._transferhis.delete(his_id)
                                logger.debug(f"[硬链接反向删除] 孤儿扫描删除转移记录: {his_id}")
                except Exception as e:
                    logger.debug(f"[硬链接反向删除] 孤儿扫描删除转移记录失败({mp_path}): {str(e)}")
                # 删除源文件
                self._delete_source_file(src_norm)
        except Exception as e:
            logger.debug(f"[硬链接反向删除] 孤儿扫描清理关联记录失败({torrent_hash}): {str(e)}")

    def _delete_source_file(self, src_path: str, fileitem: schemas.FileItem = None):
        """删除源文件（优先使用StorageChain，兼容本地和网络存储）

        :param src_path: 源文件路径字符串
        :param fileitem: 可选的FileItem对象，优先使用（包含完整存储信息）
        """
        if not src_path:
            return
        # 注意：chain.remove_torrents默认delete_file=False，不删除源文件
        # 所以这里需要显式删除源文件
        try:
            # 优先使用传入的fileitem（来自转移记录的src_fileitem）
            if fileitem is None:
                # 没有fileitem时从路径构造（默认local存储）
                fileitem = self._build_fileitem_from_path(src_path)
            if fileitem:
                # 使用StorageChain删除文件（自动处理权限、网络存储、事件通知）
                # 不预先检查os.path.exists，让StorageChain内部处理（兼容网络存储）
                self._storagechain.delete_file(fileitem)
                logger.info(f"[硬链接反向删除] 已删除源文件: {src_path}")
                self._log_action(f"删除源文件: {os.path.basename(src_path)}")
            elif os.path.exists(src_path):
                # 兜底方案：fileitem构造失败且文件存在时使用os.remove
                os.remove(src_path)
                logger.info(f"[硬链接反向删除] 已删除源文件(os): {src_path}")
                self._log_action(f"删除源文件: {os.path.basename(src_path)}")
            else:
                logger.debug(f"[硬链接反向删除] 源文件不存在且无法构造fileitem: {src_path}")
        except Exception as e:
            logger.error(f"[硬链接反向删除] 删除源文件失败: {src_path}, 错误: {str(e)}")

    @staticmethod
    def _build_fileitem_from_path(path_str: str) -> Optional[schemas.FileItem]:
        """从路径字符串构造FileItem对象（默认local存储）

        :param path_str: 文件路径
        :return: FileItem对象或None
        """
        try:
            path = Path(path_str)
            if not path.exists():
                return None
            # 判断是文件还是目录
            file_type = "file" if path.is_file() else "dir"
            extension = path.suffix[1:] if path.suffix and file_type == "file" else None
            return schemas.FileItem(
                storage="local",
                type=file_type,
                path=str(path),
                name=path.name,
                basename=path.stem,
                extension=extension,
                modify_time=path.stat().st_mtime,
            )
        except Exception as e:
            logger.debug(f"[硬链接反向删除] 构造FileItem失败({path_str}): {str(e)}")
            return None

    def _find_hash_by_src(self, src_path: str) -> str:
        """用src源文件路径反查种子hash"""
        if not src_path:
            return None
        logger.debug(f"[硬链接反向删除] 用src路径反查hash: {src_path}")
        try:
            torrent_hash = self._downloadhis.get_hash_by_fullpath(src_path)
            if torrent_hash:
                logger.info(f"[硬链接反向删除] 通过src精确路径找到hash: {torrent_hash}")
                return torrent_hash
        except Exception as e:
            logger.debug(f"[硬链接反向删除] src精确路径查询失败: {str(e)}")
        # src精确路径没找到，尝试用src的父目录查询所有下载文件
        src_parent = self._parent_dir(src_path)
        logger.debug(f"[硬链接反向删除] src精确路径未找到，尝试父目录: {src_parent}")
        try:
            src_files = self._downloadhis.get_files_by_fullpath(src_parent)
            if src_files:
                for sf in src_files:
                    sf_path = getattr(sf, 'fullpath', '') or getattr(sf, 'path', '') or ''
                    if sf_path and self._normalize_path(str(sf_path)) == self._normalize_path(src_path):
                        # 安全获取download_hash，避免str(None)='None'
                        h_raw = getattr(sf, 'download_hash', None)
                        if not h_raw and isinstance(sf, dict):
                            h_raw = sf.get('download_hash')
                        if h_raw:
                            h = str(h_raw)
                            logger.info(f"[硬链接反向删除] 通过src父目录文件列表找到hash: {h}")
                            return h
        except Exception as e:
            logger.debug(f"[硬链接反向删除] src父目录查询失败: {str(e)}")
        return None

    def _handle_torrent(self, torrent_hash: str, src_path: str, downloader_name: str = None,
                        mark_history: bool = True):
        """删除下载器里的做种任务

        :param mark_history: 是否标记下载历史删除（多次调用时只在首次调用标记）
        """
        try:
            if mark_history:
                if src_path:
                    # 有src_path时直接标记
                    try:
                        self._downloadhis.delete_file_by_fullpath(fullpath=src_path)
                    except Exception as e:
                        logger.debug(f"[硬链接反向删除] 标记下载历史删除失败(非致命): {str(e)}")
                else:
                    # src_path为空时通过hash获取文件列表逐个标记（避免下载历史变孤儿）
                    try:
                        dl_files = self._downloadhis.get_files_by_hash(torrent_hash)
                        if dl_files:
                            for df in dl_files:
                                df_path = getattr(df, 'path', '') or getattr(df, 'fullpath', '') or ''
                                if df_path:
                                    try:
                                        self._downloadhis.delete_file_by_fullpath(fullpath=str(df_path))
                                    except Exception:
                                        pass
                    except Exception as e:
                        logger.debug(f"[硬链接反向删除] 通过hash标记下载历史失败(非致命): {str(e)}")
            # downloader_name为空时查询一次（调用方未传入时兜底）
            if not downloader_name:
                try:
                    dl = self._downloadhis.get_by_hash(torrent_hash)
                    if dl:
                        # 兼容字典和对象两种访问方式
                        downloader_name = getattr(dl, 'downloader', '') or (dl.get('downloader', '') if isinstance(dl, dict) else '') or ''
                except Exception:
                    pass
            # 直接删除种子任务（chain.remove_torrents默认delete_file=False，不删除源文件）
            if downloader_name:
                logger.info(f"[硬链接反向删除] 删除做种任务: {torrent_hash}, downloader={downloader_name}")
            else:
                logger.info(f"[硬链接反向删除] 删除做种任务: {torrent_hash}, 未指定下载器将使用系统默认")
            self.chain.remove_torrents(hashs=torrent_hash, downloader=downloader_name)
            self._log_action(f"删除做种任务: {torrent_hash[:16]}...")
        except Exception as e:
            logger.error(f"[硬链接反向删除] 处理种子 {torrent_hash} 失败: {str(e)}", exc_info=True)

    def _get_torrent_save_path(self, torrent) -> str:
        # 兼容字典对象和属性对象，支持qB的save_path和Tr的download_dir
        if isinstance(torrent, dict):
            save_path = torrent.get('save_path', '') or torrent.get('download_dir', '') or ''
        else:
            save_path = getattr(torrent, 'save_path', '') or getattr(torrent, 'download_dir', '') or ''
        return str(save_path) if save_path else ''

    def _get_all_downloaders(self):
        """获取所有已启用的下载器实例列表

        :return: [(下载器名称, 下载器实例), ...]
        """
        downloader_services = []
        try:
            services = self._downloader_helper.get_services()
            if not services:
                return downloader_services
            # 标准情况：get_services() 返回字典 {name: service_info}
            if isinstance(services, dict):
                for service_name, service_info in services.items():
                    if service_info and hasattr(service_info, 'instance') and service_info.instance:
                        downloader_services.append((str(service_name), service_info.instance))
            else:
                # 兜底：非字典格式（列表/元组），遍历每个元素提取instance
                for service in services:
                    instance = getattr(service, 'instance', None) or service
                    name = getattr(service, 'name', 'default')
                    downloader_services.append((str(name), instance))
        except Exception as e:
            logger.debug(f"[硬链接反向删除] 获取下载器列表失败: {str(e)}")
        return downloader_services

    @staticmethod
    def _path_starts_with(path: str, prefix: str) -> bool:
        if not path or not prefix:
            return False
        p = path.rstrip('/')
        pre = prefix.rstrip('/')
        return p == pre or p.startswith(pre + "/")

    def scan_orphan_torrents(self):
        if not self.get_state():
            return
        monitor_dirs = self._parse_monitor_dirs()
        if not monitor_dirs:
            return

        logger.info("[硬链接反向删除] 开始扫描孤儿种子")
        self._log_action("开始扫描孤儿种子")
        self._scan_orphans_by_history(monitor_dirs)

    def _scan_orphans_by_history(self, monitor_dirs: List[str]):
        deleted_count = 0
        try:
            # 分批加载下载历史，按hash分组累积后统一处理
            # 简化策略：先全部加载并按hash分组，再逐个处理
            # 这样避免跨页hash被误处理的复杂逻辑
            BATCH_SIZE = 1000
            page = 1
            hash_groups = {}
            while True:
                batch = self._downloadhis.list_by_page(page=page, count=BATCH_SIZE)
                if not batch:
                    break
                for dl in batch:
                    if not dl or not hasattr(dl, 'download_hash') or not dl.download_hash:
                        continue
                    h = str(dl.download_hash)
                    if h not in hash_groups:
                        hash_groups[h] = []
                    hash_groups[h].append(dl)
                # 检查是否还有更多数据
                if len(batch) < BATCH_SIZE:
                    break
                page += 1
            # 所有数据加载完毕后，逐个hash处理
            for torrent_hash, download_files in hash_groups.items():
                try:
                    result = self._process_orphan_hash(torrent_hash, download_files, monitor_dirs)
                    if result:
                        deleted_count += 1
                except Exception as e:
                    logger.error(f"[硬链接反向删除] 处理hash失败 {torrent_hash}: {str(e)}")
        except Exception as e:
            logger.error(f"[硬链接反向删除] 孤儿扫描(历史记录模式)失败: {str(e)}")

        logger.info(f"[硬链接反向删除] 孤儿种子扫描完成，删除 {deleted_count} 个")
        self._log_action(f"孤儿扫描完成，删除{deleted_count}个")

    def _process_orphan_hash(self, torrent_hash: str, download_files: list, monitor_dirs: List[str]) -> bool:
        """处理单个hash对应的孤儿种子判定和删除

        :param torrent_hash: 种子hash
        :param download_files: 该hash对应的下载文件列表
        :param monitor_dirs: 监控目录列表
        :return: 是否删除了种子
        """
        if not download_files or not monitor_dirs:
            return False
        try:
            downloader_name = ''
            for df in download_files:
                if hasattr(df, 'downloader') and df.downloader:
                    downloader_name = str(df.downloader)
                    break
            monitored_total = 0
            existing_cnt = 0
            for df in download_files:
                file_path_str = None
                if hasattr(df, 'path'):
                    file_path_str = df.path
                elif hasattr(df, 'fullpath'):
                    file_path_str = df.fullpath
                if not file_path_str:
                    continue
                src_norm = self._normalize_path(str(file_path_str))
                if not self._is_media_file(src_norm):
                    continue
                in_monitor = False
                for md in monitor_dirs:
                    md_norm = self._normalize_path(md)
                    mp_path = self._map_path(src_norm, direction="to_mp")
                    if self._path_starts_with(mp_path, md_norm):
                        in_monitor = True
                        break
                    if self._path_starts_with(src_norm, md_norm):
                        in_monitor = True
                        break
                if not in_monitor:
                    continue
                monitored_total += 1
                if hasattr(df, 'state') and df.state and int(df.state) == 1:
                    existing_cnt += 1
            if monitored_total == 0:
                return False
            # 只有所有监控目录内的文件都不存在了（existing_cnt == 0）才是孤儿
            if existing_cnt > 0:
                return False
            try:
                # 删除孤儿种子（文件已全部被删除但种子还在做种）
                self.chain.remove_torrents(hashs=torrent_hash, downloader=downloader_name)
                logger.info(f"[硬链接反向删除] 删除孤儿种子: {torrent_hash[:16]}...")
                self._log_action(f"删除孤儿种子: {torrent_hash[:16]}...")
                # 同步删除该种子关联的转移记录和源文件（与主流程一致）
                self._cleanup_orphan_related_records(torrent_hash, monitor_dirs)
                return True
            except Exception as e:
                logger.error(f"[硬链接反向删除] 处理孤儿种子失败 {torrent_hash}: {str(e)}")
                return False
        except Exception:
            return False

