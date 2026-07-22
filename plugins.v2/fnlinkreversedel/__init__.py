import os
import re
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
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType


class FnLinkReverseDel(_PluginBase):
    plugin_name = "硬链接反向删除"
    plugin_desc = "监控硬链接目录，文件删除时同步删除关联种子"
    plugin_icon = "mediasyncdel.png"
    plugin_version = "5.6"
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
    _transferhis = None
    _downloadhis = None
    _storagechain = None
    _record_lock = None  # 保护 delete_records 读-改-写的专用锁

    @staticmethod
    def _safe_int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def init_plugin(self, config: dict = None):
        # 先停止旧实例的 watcher（旧工作线程为 daemon，会自行结束，finally 释放旧锁不影响新实例）
        self.stop_service()
        # 可变状态属性在实例上初始化，避免类级别共享
        self._processing_paths = set()
        self._processing_lock = threading.Lock()
        self._recent_processed = {}
        self._record_lock = threading.Lock()  # 保护 delete_records 并发读写
        self._watch_dedup = {}  # watcher 层去重：路径→时间戳（2秒窗口）
        self._transferhis = TransferHistoryOper()
        self._downloadhis = DownloadHistoryOper()
        self._storagechain = StorageChain()

        if config:
            self._enabled = bool(config.get("enabled"))
            self._monitor_dirs = config.get("monitor_dirs") or ""
            self._path_mappings = config.get("path_mappings") or ""
            self._exclude_keywords = config.get("exclude_keywords") or ""
            self._delay_delete = self._safe_int(config.get("delay_delete"), 5)
            self._orphan_scan_interval = self._safe_int(config.get("orphan_scan_interval"), 3600)
            self._force_polling = bool(config.get("force_polling"))
        # 缓存监控目录列表，避免每次事件都重新解析（性能优化）
        self._monitor_dirs_cache = self._parse_monitor_dirs() if self._monitor_dirs else []
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
                                            'rows': 3,
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
                                            'rows': 2,
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
        # 读取结构化删除记录
        records = self.get_data('delete_records') or []
        if not records:
            return [
                {
                    'component': 'VCard',
                    'props': {'variant': 'outlined', 'class': 'text-center pa-8'},
                    'content': [
                        {
                            'component': 'VCardText',
                            'props': {'class': 'pa-0'},
                            'content': [
                                {
                                    'component': 'VIcon',
                                    'props': {'size': '48', 'color': 'grey-lighten-2'},
                                    'text': 'mdi-trash-can-outline'
                                },
                                {
                                    'component': 'div',
                                    'props': {'class': 'text-body-1 text-grey mt-2'},
                                    'text': '暂无删除记录'
                                },
                                {
                                    'component': 'div',
                                    'props': {'class': 'text-caption text-grey-darken-1 mt-1'},
                                    'text': '监控目录内文件被删除时，将自动记录删除状态'
                                }
                            ]
                        }
                    ]
                }
            ]

        # 按时间降序，取最近50条
        records = sorted(records, key=lambda x: x.get('time', ''), reverse=True)[:50]

        # 统计数据
        total = len(records)
        today_str = time.strftime('%Y-%m-%d')
        today = len([r for r in records if r.get('time', '').startswith(today_str)])
        source_ok = len([r for r in records if r.get('source_deleted')])
        torrent_ok = len([r for r in records if r.get('torrent_deleted')])

        # 统计卡片行
        stats_row = {
            'component': 'VRow',
            'props': {'class': 'mb-2'},
            'content': [
                {
                    'component': 'VCol',
                    'props': {'cols': 6, 'md': 3},
                    'content': [
                        {
                            'component': 'VCard',
                            'props': {'variant': 'tonal', 'color': 'primary', 'class': 'text-center py-3'},
                            'content': [
                                {'component': 'div', 'props': {'class': 'text-h6'}, 'text': str(total)},
                                {'component': 'div', 'props': {'class': 'text-caption'}, 'text': '总删除'}
                            ]
                        }
                    ]
                },
                {
                    'component': 'VCol',
                    'props': {'cols': 6, 'md': 3},
                    'content': [
                        {
                            'component': 'VCard',
                            'props': {'variant': 'tonal', 'color': 'info', 'class': 'text-center py-3'},
                            'content': [
                                {'component': 'div', 'props': {'class': 'text-h6'}, 'text': str(today)},
                                {'component': 'div', 'props': {'class': 'text-caption'}, 'text': '今日'}
                            ]
                        }
                    ]
                },
                {
                    'component': 'VCol',
                    'props': {'cols': 6, 'md': 3},
                    'content': [
                        {
                            'component': 'VCard',
                            'props': {'variant': 'tonal', 'color': 'success', 'class': 'text-center py-3'},
                            'content': [
                                {'component': 'div', 'props': {'class': 'text-h6'}, 'text': str(source_ok)},
                                {'component': 'div', 'props': {'class': 'text-caption'}, 'text': '源文件已删'}
                            ]
                        }
                    ]
                },
                {
                    'component': 'VCol',
                    'props': {'cols': 6, 'md': 3},
                    'content': [
                        {
                            'component': 'VCard',
                            'props': {'variant': 'tonal', 'color': 'warning', 'class': 'text-center py-3'},
                            'content': [
                                {'component': 'div', 'props': {'class': 'text-h6'}, 'text': str(torrent_ok)},
                                {'component': 'div', 'props': {'class': 'text-caption'}, 'text': '做种已删'}
                            ]
                        }
                    ]
                }
            ]
        }

        # 构建删除记录卡片列表
        cards = []
        for record in records:
            # 根据媒体类型选择图标
            is_movie = record.get('type') == '电影'
            media_icon = 'mdi-movie' if is_movie else 'mdi-television-classic'
            # 构建三个状态标签
            chips = []
            for status, label in [
                (record.get('source_deleted'), '源文件'),
                (record.get('history_deleted'), '转移记录'),
                (record.get('torrent_deleted'), '做种任务'),
            ]:
                if status:
                    chips.append({
                        'component': 'VChip',
                        'props': {'size': 'x-small', 'variant': 'tonal', 'color': 'success', 'class': 'me-1 mb-1'},
                        'content': [
                            {'component': 'VIcon', 'props': {'size': 12, 'class': 'me-1'}, 'text': 'mdi-check-circle-outline'},
                            {'component': 'span', 'text': label}
                        ]
                    })
                else:
                    chips.append({
                        'component': 'VChip',
                        'props': {'size': 'x-small', 'variant': 'tonal', 'color': 'error', 'class': 'me-1 mb-1'},
                        'content': [
                            {'component': 'VIcon', 'props': {'size': 12, 'class': 'me-1'}, 'text': 'mdi-close-circle-outline'},
                            {'component': 'span', 'text': label}
                        ]
                    })

            card = {
                'component': 'VCard',
                'props': {'variant': 'outlined', 'class': 'mb-2'},
                'content': [
                    {
                        'component': 'VCardText',
                        'props': {'class': 'pa-3'},
                        'content': [
                            # 标题行：图标 + 标题 + 时间
                            {
                                'component': 'div',
                                'props': {'class': 'd-flex justify-space-between align-center mb-2'},
                                'content': [
                                    {
                                        'component': 'div',
                                        'props': {'class': 'd-flex align-center'},
                                        'content': [
                                            {
                                                'component': 'VIcon',
                                                'props': {'size': 'small', 'class': 'me-2', 'color': 'primary'},
                                                'text': media_icon
                                            },
                                            {
                                                'component': 'span',
                                                'props': {'class': 'text-body-1 font-weight-medium'},
                                                'text': record.get('title', '未知')
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'span',
                                        'props': {'class': 'text-caption text-grey'},
                                        'text': record.get('time', '')
                                    }
                                ]
                            },
                            # 状态标签行
                            {
                                'component': 'div',
                                'props': {'class': 'd-flex flex-wrap'},
                                'content': chips
                            }
                        ]
                    }
                ]
            }
            cards.append(card)

        return [stats_row] + cards

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
        # 仅停止 watcher 线程；定时任务由框架 get_service 管理，无需插件自行维护 scheduler
        self._stop_watcher()

    def _save_delete_record(self, file_path: str, source_deleted: bool,
                            history_deleted: bool, torrent_deleted: bool,
                            torrent_hash: str, title: str = None, media_type: str = None):
        """保存结构化删除记录，供详情页卡片展示

        :param title: 自定义标题，为None时从file_path自动提取
        :param media_type: 自定义类型，为None时从file_path自动推断
        """
        # 加锁保护 read-modify-write，避免并发删除时记录覆盖丢失
        if not self._record_lock:
            return
        with self._record_lock:
            try:
                records = self.get_data('delete_records') or []
                records.append({
                    'title': title or self._extract_media_title(file_path),
                    'type': media_type or self._infer_media_type(file_path),
                    'time': time.strftime('%Y-%m-%d %H:%M:%S'),
                    'source_deleted': source_deleted,
                    'history_deleted': history_deleted,
                    'torrent_deleted': torrent_deleted,
                    'torrent_hash': torrent_hash,
                })
                # 保留最近200条
                records = records[-200:]
                self.save_data('delete_records', records)
            except Exception as e:
                logger.error(f"[硬链接反向删除] 保存删除记录失败: {str(e)}", exc_info=True)

    @staticmethod
    def _extract_media_title(file_path: str) -> str:
        """从文件路径提取媒体标题（优先用父目录名，兜底用文件名去扩展名）"""
        if not file_path:
            return '未知'
        norm = file_path.replace('\\', '/').rstrip('/')
        if not norm or norm == '/':
            return '未知'
        # 父目录名通常是最干净的媒体标题
        idx = norm.rfind('/')
        if idx > 0:
            parent = norm[:idx]
            idx2 = parent.rfind('/')
            if idx2 >= 0:
                return parent[idx2 + 1:]
            return parent
        # 兜底：用文件名去掉扩展名
        basename = norm[idx + 1:] if idx >= 0 else norm
        dot_idx = basename.rfind('.')
        if dot_idx > 0:
            return basename[:dot_idx]
        return basename

    @staticmethod
    def _infer_media_type(file_path: str) -> str:
        """从路径推断媒体类型：含SxxExx为电视剧，否则为电影"""
        # 路径中含 /tv/ 或 /anime/ 或文件名含 SxxExx 模式 → 电视剧
        lower_path = file_path.lower()
        if '/tv/' in lower_path or '/anime/' in lower_path:
            return '电视剧'
        if re.search(r'[._\s][sS]\d+[eE]\d+', file_path):
            return '电视剧'
        return '电影'

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
        # 使用 init_plugin 时缓存的监控目录列表，避免每次事件都重新解析
        monitor_dirs = self._monitor_dirs_cache or self._parse_monitor_dirs()
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
                        # watcher 层去重：2秒内同一路径只处理一次
                        # 单线程内操作，无需加锁，避免跨线程锁失效问题
                        now = time.time()
                        if path_norm in self._watch_dedup:
                            if now - self._watch_dedup[path_norm] < 2:
                                logger.info(f"[硬链接反向删除] 检测到文件删除(2秒内重复，跳过): {path_norm}")
                                continue
                        self._watch_dedup[path_norm] = now
                        # 清理过期记录（超过30秒）
                        expired = [k for k, v in self._watch_dedup.items() if now - v > 30]
                        for k in expired:
                            del self._watch_dedup[k]
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
        # 双重去重：_processing_paths(处理中) + _recent_processed(120秒窗口)
        # 解决watchfiles对同一次删除触发多次事件导致的重复处理
        with self._processing_lock:
            # 清理过期的 _recent_processed 记录
            now = time.time()
            expired = [k for k, v in self._recent_processed.items() if now - v > 120]
            for k in expired:
                del self._recent_processed[k]
            # 检查1：120秒内已处理过，直接跳过
            if file_path in self._recent_processed:
                logger.info(f"[硬链接反向删除] 重复事件(120秒窗口)，跳过: {file_path}")
                return
            # 检查2：正在处理中，直接跳过
            if file_path in self._processing_paths:
                logger.info(f"[硬链接反向删除] 重复事件(处理中)，跳过: {file_path}")
                return
            self._processing_paths.add(file_path)
            self._recent_processed[file_path] = now
        try:
            self.handle_file_delete(file_path)
        finally:
            with self._processing_lock:
                self._processing_paths.discard(file_path)

    @eventmanager.register(EventType.PluginAction)
    def handle_plugin_action(self, event: Event):
        if not self.get_state():
            return
        event_data = event.event_data or {}
        if event_data.get("action") == "fnlink_scan":
            self.scan_orphan_torrents()

    def _map_path(self, path_str: str, direction: str = "to_src") -> str:
        path_norm = self._normalize_path(path_str)
        # 空路径或无映射配置时直接返回
        if not path_norm or not self._path_mappings:
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
                # 必须用 + "/" 边界判定，避免 /video/link 误匹配 /video/link2
                if left and (path_norm == left or path_norm.startswith(left + "/")):
                    return right + path_norm[len(left):]
            elif direction == "to_mp":
                # 源文件路径 → 监控路径：right=下载源目录, left=监控目录
                if right and (path_norm == right or path_norm.startswith(right + "/")):
                    return left + path_norm[len(right):]
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
        """处理硬链接文件删除事件（方案B：复用后端逻辑）

        流程：
        1. 通过 dest 路径查转移记录（含 src_fileitem/download_hash/downloader）
        2. 调用 StorageChain.delete_media_file 删源文件（与后端 API 一致，自动删空目录）
        3. 调用 DownloadFiles.delete_by_fullpath 删下载文件记录（state=0）
        4. 调用 TransferHistoryOper.delete 删转移记录
        5. 检查种子所有文件是否都已删除，若是才调用 chain.remove_torrents 删做种任务
        """
        # 去重检查已在 _async_handle_delete 入口完成（_recent_processed + _processing_paths 双重保险）
        logger.info(f"[硬链接反向删除] 处理文件删除: {file_path}")

        # 跟踪三项删除状态，用于详情页展示
        source_deleted = False
        history_deleted = False
        torrent_deleted = False
        torrent_hash_short = ""

        # 步骤1：查转移记录（dest = 硬链接路径）
        histories = self._find_transfer_history(file_path)
        if not histories:
            logger.warning(f"[硬链接反向删除] 未找到转移记录，跳过: {file_path}")
            # 即使未找到转移记录也保存一条记录，标记全部失败
            self._save_delete_record(file_path, False, False, False, "")
            return

        # 步骤2-4：逐个处理转移记录（删源文件、删下载文件记录、删转移记录）
        # 统计成功/总数，便于排查"显示绿色但实际只删部分"的情况
        total_histories = len(histories)
        source_ok_cnt = 0
        history_ok_cnt = 0
        processed_hashes = set()
        for history in histories:
            try:
                result = self._delete_history_and_related(history)
                if result:
                    if result.get("source_deleted"):
                        source_deleted = True
                        source_ok_cnt += 1
                    if result.get("history_deleted"):
                        history_deleted = True
                        history_ok_cnt += 1
                    dh = result.get("download_hash")
                    if dh:
                        processed_hashes.add(dh)
            except Exception as e:
                logger.error(f"[硬链接反向删除] 处理转移记录失败(id={getattr(history, 'id', '?')}): {str(e)}", exc_info=True)
        if total_histories > 1:
            logger.info(f"[硬链接反向删除] 转移记录处理完成: 源文件 {source_ok_cnt}/{total_histories}, 转移记录 {history_ok_cnt}/{total_histories}")

        # 步骤5：检查种子所有文件是否都已删除，若是才删做种任务（避免误删整季）
        # 多个 hash 都成功删除时，torrent_hash_short 拼接所有 hash（截断）
        deleted_hashes = []
        for download_hash in processed_hashes:
            try:
                if self._remove_torrent_if_all_deleted(download_hash):
                    torrent_deleted = True
                    deleted_hashes.append(download_hash[:16])
            except Exception as e:
                logger.error(f"[硬链接反向删除] 删除做种任务失败(hash={download_hash}): {str(e)}")
        if deleted_hashes:
            # 多个 hash 用逗号拼接，避免只显示最后一个
            torrent_hash_short = ",".join(deleted_hashes) + "..."

        # 保存结构化删除记录，供详情页展示
        self._save_delete_record(file_path, source_deleted, history_deleted, torrent_deleted, torrent_hash_short)

    def _find_transfer_history(self, dest_path: str) -> List:
        """通过 dest 路径查找转移记录，多级查询策略

        查询顺序：
        1. dest 精确匹配（get_by_dest 返回单条，再用 list_by_hash 扩展同 hash 的所有记录）
        2. 路径映射推断 src 后用 get_by_src 反查（同样用 list_by_hash 扩展）

        注意：移除了"父目录查询"策略，因为父目录的转移记录 src 可能是目录而非文件，
              会导致 StorageChain 误删整个目录（参考 P0 问题3）
        """
        # 策略1：dest 精确匹配
        try:
            history = self._transferhis.get_by_dest(dest_path)
            if history:
                logger.info(f"[硬链接反向删除] 通过dest精确路径找到转移记录: id={getattr(history, 'id', '?')}, src={getattr(history, 'src', '')}, download_hash={getattr(history, 'download_hash', None)}")
                # 用 list_by_hash 扩展：同一 hash 可能有多条转移记录（如多文件种子每集一条）
                # 关键：list_by_hash 单独 try-except，失败不影响返回 [history]
                download_hash = getattr(history, 'download_hash', None)
                if download_hash:
                    try:
                        all_histories = self._transferhis.list_by_hash(str(download_hash))
                        if all_histories and len(all_histories) > 1:
                            logger.info(f"[硬链接反向删除] 同hash共{len(all_histories)}条转移记录，全部处理")
                            return all_histories
                    except Exception as e:
                        logger.warning(f"[硬链接反向删除] list_by_hash扩展查询失败(非致命，使用单条记录): {str(e)}")
                return [history]
        except Exception as e:
            logger.error(f"[硬链接反向删除] dest精确查询失败: {str(e)}", exc_info=True)

        # 策略2：路径映射推断 src，再用 get_by_src 反查
        mapped_src = self._map_path(dest_path, direction="to_src")
        if mapped_src != dest_path:
            try:
                history = self._transferhis.get_by_src(mapped_src)
                if history:
                    logger.info(f"[硬链接反向删除] 通过路径映射+get_by_src找到转移记录: id={getattr(history, 'id', '?')}")
                    download_hash = getattr(history, 'download_hash', None)
                    if download_hash:
                        try:
                            all_histories = self._transferhis.list_by_hash(str(download_hash))
                            if all_histories and len(all_histories) > 1:
                                logger.info(f"[硬链接反向删除] 同hash共{len(all_histories)}条转移记录，全部处理")
                                return all_histories
                        except Exception as e:
                            logger.warning(f"[硬链接反向删除] list_by_hash扩展查询失败(非致命，使用单条记录): {str(e)}")
                    return [history]
            except Exception as e:
                logger.error(f"[硬链接反向删除] 路径映射反查失败: {str(e)}", exc_info=True)

        logger.warning(f"[硬链接反向删除] 未找到转移记录: {dest_path}")
        return []

    def _delete_history_and_related(self, history) -> Dict[str, Any]:
        """删除单条转移记录及其关联资源（与后端 DELETE /api/v1/history/transfer 行为一致）

        顺序（参考后端 history.py:221）：
        0. download_hash 为空时先反查（必须在 delete_file_by_fullpath 之前，否则 state=0 后查不到）
        1. 删源文件（StorageChain.delete_media_file，自动处理空目录）
        2. 删下载文件记录（DownloadFiles.delete_by_fullpath，state=0）
        3. 删转移记录（TransferHistory.delete）

        注意：不再在此方法删除做种任务，由调用方统一判断"所有文件是否都已删除"后决定是否删除种子

        :return: 包含 download_hash/source_deleted/history_deleted 的状态字典
        """
        # 提取字段
        his_id = getattr(history, 'id', None)
        src_path = getattr(history, 'src', '') or ''
        download_hash = getattr(history, 'download_hash', None) or ''
        src_fileitem_dict = getattr(history, 'src_fileitem', None) or {}
        # 跟踪三项删除状态，供详情页展示
        source_deleted = False
        history_deleted = False

        if download_hash:
            download_hash = str(download_hash)

        # 步骤0：download_hash 为空时，用 src_path 反查 downloadhis 获取 hash
        # 关键：必须在 delete_file_by_fullpath 之前执行！
        # 因为 delete_file_by_fullpath 会将 DownloadFiles.state 改为 0，
        # 如果 get_hash_by_fullpath 内部过滤 state=1，反查会失败
        if not download_hash and src_path:
            try:
                src_posix = Path(src_path).as_posix()
                # get_hash_by_fullpath 内部按 fullpath == src_posix 查询
                hash_from_db = self._downloadhis.get_hash_by_fullpath(src_posix)
                if hash_from_db:
                    download_hash = str(hash_from_db)
                    logger.info(f"[硬链接反向删除] 通过src反查到hash: {download_hash}")
            except Exception as e:
                logger.warning(f"[硬链接反向删除] src反查hash失败: {str(e)}")

        # 步骤1：删除源文件（使用 StorageChain，与后端一致）
        if src_path:
            src_fileitem = None
            # 优先用转移记录中的 src_fileitem（包含完整存储信息，与后端 API 一致）
            if isinstance(src_fileitem_dict, dict) and src_fileitem_dict:
                try:
                    src_fileitem = schemas.FileItem(**src_fileitem_dict)
                except Exception as e:
                    logger.debug(f"[硬链接反向删除] 构造src_fileitem失败: {str(e)}")
            # 兜底：从路径构造（默认local存储，不检查文件是否存在，与后端一致）
            if src_fileitem is None and src_path:
                src_fileitem = self._build_fileitem_from_path(src_path)
            if src_fileitem:
                try:
                    # delete_media_file 会自动：删文件 → 删空父目录（不删媒体库根目录）
                    state = self._storagechain.delete_media_file(src_fileitem)
                    if state:
                        source_deleted = True
                        logger.info(f"[硬链接反向删除] 已删除源文件: {src_path}")
                    else:
                        logger.warning(f"[硬链接反向删除] 源文件删除失败(可能已不存在): {src_path}")
                except Exception as e:
                    logger.error(f"[硬链接反向删除] 删除源文件异常: {src_path}, {str(e)}")

        # 步骤2：删除下载文件记录（state=0，与后端一致）
        # 关键：后端 DownloadFiles.fullpath 字段使用 POSIX 风格（Path.as_posix()），
        # delete_by_fullpath 用 == 精确匹配，所以这里必须用 Path(src_path).as_posix() 转换
        if src_path:
            try:
                # 统一转换为 POSIX 风格，与后端 fullpath 字段存储格式一致
                src_posix = Path(src_path).as_posix()
                self._downloadhis.delete_file_by_fullpath(fullpath=src_posix)
                logger.info(f"[硬链接反向删除] 已标记下载文件记录删除: {src_posix}")
            except Exception as e:
                logger.error(f"[硬链接反向删除] 标记下载文件记录失败: {str(e)}")

        # 步骤3：删除转移记录
        if his_id is not None:
            try:
                self._transferhis.delete(his_id)
                history_deleted = True
                logger.info(f"[硬链接反向删除] 已删除转移记录: {his_id}")
            except Exception as e:
                logger.error(f"[硬链接反向删除] 删除转移记录失败: {str(e)}")

        return {
            "download_hash": download_hash or None,
            "source_deleted": source_deleted,
            "history_deleted": history_deleted,
        }

    def _remove_torrent_if_all_deleted(self, download_hash: str) -> bool:
        """检查种子所有文件是否都已删除（state=0），若是才删除做种任务

        避免误删整季：多文件种子中，只有当所有文件都被标记删除（state=0）时才删种子任务。
        否则保留种子，让其他文件继续做种。

        :param download_hash: 种子hash
        :return: 是否成功删除了做种任务
        """
        if not download_hash:
            return False
        try:
            # 查询该 hash 的所有下载文件记录（不传 state 参数，查全部）
            dl_files = self._downloadhis.get_files_by_hash(download_hash)
            if not dl_files:
                logger.info(f"[硬链接反向删除] hash无下载文件记录，跳过删种子: {download_hash[:16]}...")
                return False
            # 检查是否所有文件都已标记删除（state != 1）
            has_active = False
            total_files = len(dl_files)
            active_files = 0
            for df in dl_files:
                # DownloadFiles.state: 0=已删除, 1=正常
                # 兼容 int/str/None 多种类型
                df_state_raw = getattr(df, 'state', None)
                try:
                    df_state = int(df_state_raw) if df_state_raw is not None else 1
                except (TypeError, ValueError):
                    df_state = 1
                if df_state == 1:
                    has_active = True
                    active_files += 1
                    df_fullpath = getattr(df, 'fullpath', '') or ''
                    logger.debug(f"[硬链接反向删除] 仍有活跃文件: state={df_state}, fullpath={df_fullpath}")
            if has_active:
                logger.info(f"[硬链接反向删除] 种子仍有{active_files}/{total_files}个活跃文件(state=1)，保留种子: {download_hash[:16]}...")
                return False
            # 所有文件都已删除，安全删除种子任务
            # 查询 downloader 名称
            downloader_name = ''
            try:
                dl = self._downloadhis.get_by_hash(download_hash)
                if dl:
                    downloader_name = getattr(dl, 'downloader', '') or ''
            except Exception as e:
                logger.warning(f"[硬链接反向删除] 查询下载器名称失败(hash={download_hash[:16]}...): {str(e)}")
            try:
                if downloader_name:
                    logger.info(f"[硬链接反向删除] 删除做种任务: {download_hash}, downloader={downloader_name}, 共{total_files}个文件全部已删除")
                else:
                    logger.info(f"[硬链接反向删除] 删除做种任务: {download_hash}, 未指定下载器将使用系统默认, 共{total_files}个文件全部已删除")
                # delete_file=False 不删源文件（源文件已在前面步骤删除）
                self.chain.remove_torrents(hashs=download_hash, downloader=downloader_name)
                return True
            except Exception as e:
                logger.error(f"[硬链接反向删除] 删除做种任务失败: {str(e)}")
                return False
        except Exception as e:
            logger.error(f"[硬链接反向删除] 检查种子文件状态失败: {str(e)}")
            return False

    def _cleanup_orphan_related_records(self, torrent_hash: str, monitor_dirs: List[str]) -> Tuple[bool, bool]:
        """删除孤儿种子关联的转移记录和源文件（孤儿扫描专用，方案B）

        复用 _find_transfer_history + _delete_history_and_related，与主流程一致
        增加监控目录校验：仅处理监控目录内的文件，避免误删非监控目录的转移记录

        :return: (source_deleted, history_deleted) 聚合结果，任一记录删除成功即为 True
        """
        source_deleted = False
        history_deleted = False
        try:
            dl_files = self._downloadhis.get_files_by_hash(torrent_hash)
            if not dl_files:
                return source_deleted, history_deleted
            # 预计算监控目录标准化形式
            monitor_dirs_norm = [self._normalize_path(md) for md in monitor_dirs]
            for df in dl_files:
                # DownloadFiles 模型字段：downloader/download_hash/fullpath/savepath/filepath/torrentname/state
                src_path = getattr(df, 'fullpath', '') or ''
                if not src_path:
                    continue
                src_norm = self._normalize_path(str(src_path))
                if not self._is_media_file(src_norm):
                    continue
                # 校验：源文件路径映射后必须在监控目录内，避免误删非监控目录的转移记录
                mp_path = self._map_path(src_norm, direction="to_mp")
                in_monitor = False
                for md_norm in monitor_dirs_norm:
                    if self._path_starts_with(mp_path, md_norm) or self._path_starts_with(src_norm, md_norm):
                        in_monitor = True
                        break
                if not in_monitor:
                    logger.debug(f"[硬链接反向删除] 孤儿扫描跳过非监控目录文件: {src_norm}")
                    continue
                # 查询并删除转移记录（仅处理监控目录内的）
                histories = self._find_transfer_history(mp_path)
                for history in histories:
                    try:
                        result = self._delete_history_and_related(history)
                        if result:
                            if result.get("source_deleted"):
                                source_deleted = True
                            if result.get("history_deleted"):
                                history_deleted = True
                    except Exception as e:
                        logger.warning(f"[硬链接反向删除] 孤儿扫描删除转移记录失败: {str(e)}")
        except Exception as e:
            logger.warning(f"[硬链接反向删除] 孤儿扫描清理关联记录失败({torrent_hash}): {str(e)}")
        return source_deleted, history_deleted

    @staticmethod
    def _build_fileitem_from_path(path_str: str) -> Optional[schemas.FileItem]:
        """从路径字符串构造FileItem对象（默认local存储）

        :param path_str: 文件路径
        :return: FileItem对象或None
        """
        try:
            path = Path(path_str)
            # 不检查文件是否存在，与后端 API 一致
            # StorageChain.delete_media_file 内部会处理文件不存在的情况
            # 检查 exists() 会在网络存储场景下误判（如 SMB/NFS 未挂载时返回 False）
            # 判断是文件还是目录（无法判断时默认按文件处理）
            file_type = "file"
            try:
                file_type = "file" if path.is_file() else "dir"
            except Exception:
                pass
            extension = path.suffix[1:] if path.suffix and file_type == "file" else None
            return schemas.FileItem(
                storage="local",
                type=file_type,
                path=path.as_posix(),
                name=path.name,
                basename=path.stem,
                extension=extension,
            )
        except Exception as e:
            logger.debug(f"[硬链接反向删除] 构造FileItem失败({path_str}): {str(e)}")
            return None

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
        self._scan_orphans_by_history(monitor_dirs)

    def _scan_orphans_by_history(self, monitor_dirs: List[str]):
        """流式扫描孤儿种子，避免一次性加载所有历史记录导致内存压力

        策略：分批加载 + 按hash分组 + 处理完即释放
        - 每批 1000 条，加载后按 hash 分组
        - 同一 hash 的文件可能跨页，所以用字典累积
        - 每处理完一个 hash 立即从字典移除，释放内存
        """
        deleted_count = 0
        try:
            BATCH_SIZE = 1000
            page = 1
            hash_groups = {}
            while True:
                batch = self._downloadhis.list_by_page(page=page, count=BATCH_SIZE)
                if not batch:
                    break
                # 当前批次按 hash 分组（累积到 hash_groups，最后统一处理）
                for dl in batch:
                    if not dl or not hasattr(dl, 'download_hash') or not dl.download_hash:
                        continue
                    h = str(dl.download_hash)
                    if h not in hash_groups:
                        hash_groups[h] = []
                    hash_groups[h].append(dl)
                # 本批次不足一页说明已到末尾
                if len(batch) < BATCH_SIZE:
                    break
                page += 1
            # 所有数据加载完毕后，逐个hash处理（处理完即释放）
            for torrent_hash in list(hash_groups.keys()):
                download_files = hash_groups.pop(torrent_hash, [])
                try:
                    result = self._process_orphan_hash(torrent_hash, download_files, monitor_dirs)
                    if result:
                        deleted_count += 1
                except Exception as e:
                    logger.error(f"[硬链接反向删除] 处理hash失败 {torrent_hash}: {str(e)}")
        except Exception as e:
            logger.error(f"[硬链接反向删除] 孤儿扫描(历史记录模式)失败: {str(e)}")

        logger.info(f"[硬链接反向删除] 孤儿种子扫描完成，删除 {deleted_count} 个")

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
            # 预计算监控目录的标准化形式，避免在内层循环重复 normalize
            monitor_dirs_norm = [self._normalize_path(md) for md in monitor_dirs]
            monitored_total = 0
            existing_cnt = 0
            for df in download_files:
                # DownloadFiles 模型字段：downloader/download_hash/fullpath/savepath/filepath/torrentname/state
                # 没有 path 字段，直接用 fullpath（完整路径）
                file_path_str = getattr(df, 'fullpath', '') or ''
                if not file_path_str:
                    continue
                src_norm = self._normalize_path(str(file_path_str))
                if not self._is_media_file(src_norm):
                    continue
                # mp_path 依赖 src_norm，每个文件需计算一次（在文件循环内、监控目录循环外）
                mp_path = self._map_path(src_norm, direction="to_mp")
                in_monitor = False
                for md_norm in monitor_dirs_norm:
                    if self._path_starts_with(mp_path, md_norm) or self._path_starts_with(src_norm, md_norm):
                        in_monitor = True
                        break
                if not in_monitor:
                    continue
                monitored_total += 1
                # state 兼容 int/str/None，异常时默认 1（存在），避免误判为孤儿
                df_state_raw = getattr(df, 'state', None)
                try:
                    df_state = int(df_state_raw) if df_state_raw is not None else 1
                except (TypeError, ValueError):
                    df_state = 1
                if df_state == 1:
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
                # 同步删除该种子关联的转移记录和源文件，并收集实际删除状态
                orphan_source_deleted, orphan_history_deleted = self._cleanup_orphan_related_records(torrent_hash, monitor_dirs)
                # 提取标题和路径用于记录
                orphan_title = ''
                orphan_path = ''
                for df in download_files:
                    tn = getattr(df, 'torrentname', '') or ''
                    if tn:
                        orphan_title = tn
                    fp = getattr(df, 'fullpath', '') or ''
                    if fp:
                        orphan_path = str(fp)
                    if orphan_title and orphan_path:
                        break
                if not orphan_path and download_files:
                    orphan_path = getattr(download_files[0], 'fullpath', '') or torrent_hash
                # 保存孤儿扫描删除记录，做种任务已确认删除=True，源文件/转移记录用实际结果
                self._save_delete_record(
                    orphan_path or torrent_hash,
                    orphan_source_deleted, orphan_history_deleted, True, torrent_hash[:16] + '...',
                    title=orphan_title or None
                )
                return True
            except Exception as e:
                logger.error(f"[硬链接反向删除] 处理孤儿种子失败 {torrent_hash}: {str(e)}")
                return False
        except Exception:
            return False

