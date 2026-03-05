# -*-coding:utf8-*-

import re
import time
import random
import os
import requests
from urllib.parse import urlparse, parse_qs, unquote
from fake_useragent import UserAgent
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
import threading

try:
    import yaml  # 复用项目依赖；如环境没装则继续用默认配置
except Exception:
    yaml = None

try:
    import msvcrt  # Windows: 无回车按键检测（按 q 请求停止）
except Exception:
    msvcrt = None


# 加载关键词列表
def load_keywords():
    key_words = []
    # 使用相对路径，确保在不同目录下运行都能找到文件
    import os
    base_dir = os.path.dirname(os.path.abspath(__file__))
    kw_city_path = os.path.join(base_dir, 'kw_city')
    kw_hospital_path = os.path.join(base_dir, 'kw_hospital.txt')
    
    with open(kw_city_path, 'r', encoding='utf-8') as f_city, open(kw_hospital_path, 'r', encoding='utf-8') as f_hospital:
        city_list = [city.strip() for city in f_city.readlines()]
        hospital_list = [hospital.strip() for hospital in f_hospital.readlines()]
        if city_list and hospital_list:
            key_words = [f"{city}{hospital}" for city in city_list for hospital in hospital_list]

    return key_words


def headers():
    return {
        "User-Agent": UserAgent().random,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
                  "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
        "Accept-Encoding": "gzip, deflate"
    }


DESKTOP_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]


def baidu_search_headers(user_agent: str | None = None):
    """百度搜索专用请求头，固定桌面 UA，尽量命中 PC 结果页。

    注意：同一个 session/keyword 内，建议传入固定的 user_agent，避免每次请求 UA 都变化导致更容易触发风控。
    """
    ua = user_agent or random.choice(DESKTOP_UAS)
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
                  "image/apng,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Accept-Encoding": "gzip, deflate",
    }


def normalize_baidu_candidate_link(url):
    """标准化百度候选链接，仅保留 www.baidu.com/baidu.php?url= 格式。"""
    u = (url or '').strip()
    if not u:
        return ''

    if u.startswith('//'):
        u = 'https:' + u

    if u.startswith('/'):
        if u.startswith('/baidu.php?url='):
            u = 'https://www.baidu.com' + u
        else:
            return ''

    if u.startswith('http://'):
        u = 'https://' + u[len('http://'):]

    # 统一移动域名到 PC 域名
    u = re.sub(
        r'^https://m\.baidu\.com/baidu\.php\?url=',
        'https://www.baidu.com/baidu.php?url=',
        u,
        flags=re.IGNORECASE,
    )

    if not re.match(r'^https://www\.baidu\.com/baidu\.php\?url=', u, re.IGNORECASE):
        return ''

    return u


def baidu_candidate_fingerprint(link: str, prefix_len: int = 16) -> str:
    """对 baidu.php?url= 候选链接做“早期指纹”，用于在解析跳转前快速去重。

    背景：同一广告在页面内可能以多个位置/标签出现，且 baidu.php?url= 参数会携带不同追踪片段，
    造成候选链接看起来不同但最终落地页相同。

    策略：提取 query 里的 url=token，对 token URLDecode 后取前 N 位作为指纹（可配置）。
    """
    u = (link or '').strip()
    if not u:
        return ''

    u = normalize_baidu_candidate_link(u) or u

    try:
        parsed = urlparse(u)
        qs = parse_qs(parsed.query or '')
        token = (qs.get('url') or [''])[0] or ''
        token = unquote(token)
    except Exception:
        m = re.search(r'[?&]url=([^&]+)', u)
        token = unquote(m.group(1)) if m else ''

    token = (token or '').strip()
    if not token:
        return ''

    # 尽量稳定：仅保留常见 token 字符集，避免因编码差异导致指纹抖动
    safe = re.sub(r'[^A-Za-z0-9_\-]+', '', token)
    if not safe:
        safe = token

    try:
        n = int(prefix_len)
    except Exception:
        n = 16
    n = max(4, min(128, n))

    return safe[:n]


def proxies():
    return {
        # 如需使用代理请自行替换，这里有一些不太好用的免费代理
        # - https://www.docip.net/#index
        # - https://openproxy.space/list

        # "http": "x.x.x.x:xx",
        # "https": "x.x.x.x:xx",
    }


def get_imid(url):
    """从URL中提取 imid 参数（容错：遇到 `imid=xxx?yyy` 也只取 xxx）。"""
    match = re.search(r'imid=([\w-]+)', url or '')
    if match:
        return match.group(1)
    return None


def canonicalize_ada_url(url: str) -> str:
    """把抓到的 ada 链接归一化，移除无用参数，只保留 `?imid=`。

    目标：避免出现 `...imid=xxx?bdjj...&ch=...` 这类脏 URL，保证写入 api.txt 可稳定去重。
    """
    clean = (url or '').strip()
    if not clean:
        return ''

    imid = get_imid(clean)
    if not imid:
        return clean

    try:
        parsed = urlparse(clean)
        host = (parsed.netloc or '').lower()
        path = parsed.path or ''
        # 只对 ada 域名做归一化
        if 'ada.baidu.com' not in host:
            return clean
        if not path.startswith('/'):
            path = '/' + path
        return f"https://ada.baidu.com{path}?imid={imid}"
    except Exception:
        # 解析失败则至少返回最关键的 imid
        m = re.search(r'(https?://ada\.baidu\.com[^\s"\']+?)\?imid=[\w-]+', clean)
        if m:
            base = m.group(1)
            return f"{base}?imid={imid}"
        return clean


def jitter_sleep(seconds_range):
    """按区间随机 sleep；传 None/非法值则不 sleep。"""
    try:
        if not seconds_range:
            return
        a, b = seconds_range
        a = float(a)
        b = float(b)
        if b < a:
            a, b = b, a
        if b <= 0:
            return
        time.sleep(random.uniform(max(0.0, a), b))
    except Exception:
        return


def load_scrape_config(project_root: str) -> dict:
    """加载抓取配置：优先读取项目根目录 config.yaml 的 catchad 部分，其次 catchad/config.yaml。"""
    defaults = {
        'max_workers': 1,
        'max_page': 2,
        # 每页“唯一广告”的目标数量（按 imid 去重后计数）。
        # 说明：百度结果页里可能出现大量看起来不同的 baidu.php?url=，但实际打开是同一广告。
        # 这里用“解析后的 imid 去重”来避免这些重复链接占用 candidate_links_limit 配额。
        'candidate_links_limit': 10,
        # 早期去重固定启用（不需要手动配置），仅保留前缀长度可调。
        'candidate_fingerprint_prefix_len': 16,
        'delay_per_keyword_range': (1.0, 3.0),
        'delay_between_pages_range': (0.8, 2.0),
        'delay_between_resolves_range': (0.2, 0.8),
        'resume_enabled': True,
        'resume_file': os.path.join(project_root, 'catchad', 'done_keywords.txt'),
        'api_file': os.path.join(project_root, 'api.txt'),
        # requests 超时：支持 YAML 里写成 [connect, read]
        'request_timeout': (5, 10),
        # 命中风控页后的动作：目前实现 stop（全局停止并取消后续 keyword）
        'risk_control_action': 'stop',
        # 兼容旧配置：如果用户已有该字段则生效；否则不设上限。
        # 每页最多扫描多少条候选跳转链接（None 表示不设上限）
        'candidate_links_scan_limit': None,
    }

    if yaml is None:
        return defaults

    def _merge_from(path: str):
        try:
            if not os.path.exists(path):
                return
            with open(path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                return
            cfg = data.get('catchad')
            if isinstance(cfg, dict):
                defaults.update(cfg)
        except Exception:
            return

    _merge_from(os.path.join(project_root, 'config.yaml'))
    _merge_from(os.path.join(project_root, 'catchad', 'config.yaml'))

    return defaults


def is_baidu_security_verify_page(html: str) -> bool:
    """检测是否命中百度安全验证/风控页面。"""
    text = html or ''
    # decoded.html 里出现过的特征：title=百度安全验证 + ppui-static-wap 的 mkdjump 资源
    if 'ppui-static-wap.cdn.bcebos.com/static/touch/css/api/mkdjump' in text:
        return True

    m = re.search(r'<title>(.*?)</title>', text, re.IGNORECASE | re.DOTALL)
    if m:
        title = (m.group(1) or '').strip()
        if ('安全验证' in title) or ('百度安全' in title):
            return True

    return False


def extract_baidu_result_links(html):
    """从百度搜索结果页提取可点击跳转链接（按页面顺序）"""
    import html as html_lib

    decoded_html = html_lib.unescape(html or '')
    links = []
    # 百度结果页中 link 可能是绝对地址、相对地址、JSON 字段等多种形态
    patterns = [
        r'href=["\'](?P<u>https?://(?:www|m)\.baidu\.com/baidu\.php\?url=[^"\'\s>]+)["\']',
        r'href=["\'](?P<u>/baidu\.php\?url=[^"\'\s>]+)["\']',
        r'["\']url["\']\s*:\s*["\'](?P<u>https?://(?:www|m)\.baidu\.com/baidu\.php\?url=[^"\']+)["\']',
        r'["\']url["\']\s*:\s*["\'](?P<u>/baidu\.php\?url=[^"\']+)["\']',
        # r'["\']mu["\']\s*:\s*["\'](?P<u>https?://[^"\']+)["\']',
    ]

    for pattern in patterns:
        for m in re.finditer(pattern, decoded_html):
            u = normalize_baidu_candidate_link(m.group('u'))
            if not u:
                continue
            links.append(u)

    # 去重但保序
    unique_links = []
    seen = set()
    for link in links:
        if link not in seen:
            seen.add(link)
            unique_links.append(link)
    return unique_links


def resolve_baidu_link(
    baidu_link,
    stop_event,
    session: requests.Session | None = None,
    user_agent: str | None = None,
    timeout=(5, 10),
):
    """打开百度跳转链接，拿到真实落地页 URL（尽量复用 session cookie）。

    若在跳转过程中命中风控页，会设置 stop_event 触发全局停止，避免继续无意义访问。
    """
    if stop_event.is_set():
        return None

    client = session or requests

    try:
        resp = client.get(
            baidu_link,
            headers=baidu_search_headers(user_agent=user_agent),
            proxies=proxies(),
            timeout=timeout,
            allow_redirects=True,
        )

        # 风控页常见跳转域名（兜底）
        if 'wappass.baidu.com' in (resp.url or ''):
            stop_event.set()
            return None

        if is_baidu_security_verify_page(resp.text):
            stop_event.set()
            return None

        final_url = (resp.url or '').strip()
        if 'ada.baidu.com' in final_url and 'imid=' in final_url:
            return final_url

        # 部分场景会在响应内容里带真实链接，做一次兜底提取
        text = (resp.text or '').replace('\\/', '/')
        m = re.search(r'https?://ada\.baidu\.com/site/[\w.-]+(?:/xyl)?\?[^"\s<>]*imid=[\w-]+', text)
        if m:
            return m.group(0)
    except Exception:
        pass

    return None


def fetch(keyword, stop_event, cfg: dict):
    if stop_event.is_set():
        return []

    delay_range = cfg.get('delay_per_keyword_range')
    delay = random.uniform(float(delay_range[0]), float(delay_range[1])) if delay_range else 0.0
    print(f"关键字: {keyword} \t {delay:.2f} 秒后开始提取", flush=True)
    time.sleep(max(0.0, delay))

    if stop_event.is_set():
        return []

    # 统一本 keyword 的 UA，避免一次 keyword 内指纹频繁变化
    ua = random.choice(DESKTOP_UAS)

    # requests timeout：支持 [connect, read]
    timeout = cfg.get('request_timeout') or (5, 10)
    try:
        if isinstance(timeout, (list, tuple)) and len(timeout) == 2:
            timeout = (float(timeout[0]), float(timeout[1]))
        else:
            timeout = (5, 10)
    except Exception:
        timeout = (5, 10)

    max_page = int(cfg.get('max_page', 2) or 2)
    search_url = 'https://www.baidu.com/s'
    results = []

    # 使用 session 复用 cookie，降低触发风控概率
    s = requests.Session()

    try:
        # 预热：先访问首页拿 BAIDUID 等 cookie
        try:
            s.get(
                'https://www.baidu.com/',
                headers=baidu_search_headers(user_agent=ua),
                proxies=proxies(),
                timeout=timeout,
                allow_redirects=True,
            )
        except Exception:
            pass

        for page in range(max_page):
            if stop_event.is_set():
                break

            # 翻页之间加延迟，避免过快触发风控
            jitter_sleep(cfg.get('delay_between_pages_range'))

            response = s.get(
                search_url,
                params={
                    'wd': keyword,
                    'pn': page * 10,
                    'ie': 'utf-8',
                    'tn': 'baidu',
                    'rsv_dl': 'pc',
                },
                headers={
                    **baidu_search_headers(user_agent=ua),
                    'Referer': 'https://www.baidu.com/',
                },
                proxies=proxies(),
                timeout=timeout,
                allow_redirects=True,
            )

            if ('wappass.baidu.com' in (response.url or '')) or is_baidu_security_verify_page(response.text):
                page_title = ''
                m_title = re.search(r'<title>(.*?)</title>', response.text or '', re.IGNORECASE | re.DOTALL)
                if m_title:
                    page_title = m_title.group(1).strip().replace('\n', ' ')
                print(
                    f"关键字: {keyword} \t 命中百度安全验证/风控页面，title={page_title}，将触发全局停止（避免继续无意义访问）。"
                    f"\n建议：更换高质量代理/降低并发（max_workers=1）/降低翻页（max_page=1）/或改用浏览器方式抓取",
                    flush=True,
                )
                if str(cfg.get('risk_control_action') or 'stop').lower() == 'stop':
                    stop_event.set()
                break

            # 第一步：先提取结果链接（仅保留 www.baidu.com/baidu.php?url=）
            candidate_links = extract_baidu_result_links(response.text)

            # 目标：每页最多拿到多少条“唯一广告”（按解析后的 imid 去重计数）
            limit = int(cfg.get('candidate_links_limit', 10) or 10)
            limit = max(1, limit)

            # 扫描上限：默认不设上限（无需手动配置）。
            # 仅当用户在 config.yaml 里显式配置了 candidate_links_scan_limit 才会截断。
            scan_limit_cfg = cfg.get('candidate_links_scan_limit', None)
            scan_limit = None
            if scan_limit_cfg is not None and str(scan_limit_cfg).strip() != '':
                try:
                    scan_limit = int(scan_limit_cfg)
                except Exception:
                    scan_limit = None
            if scan_limit is not None:
                scan_limit = max(limit, max(1, scan_limit))
                candidate_links = candidate_links[:scan_limit]

            # 早期去重：
            # 不打开链接，先按 baidu.php?url= token 前缀做一次去重，避免同一广告重复几十次导致大量无意义 resolve。
            if candidate_links:
                prefix_len = cfg.get('candidate_fingerprint_prefix_len', 16)
                seen_fp = set()
                filtered = []
                dropped = 0
                for c in candidate_links:
                    fp = baidu_candidate_fingerprint(c, prefix_len=prefix_len)
                    if fp and fp in seen_fp:
                        dropped += 1
                        continue
                    if fp:
                        seen_fp.add(fp)
                    filtered.append(c)
                if dropped:
                    print(f"关键字: {keyword} \t page={page} 早期指纹去重丢弃 {dropped} 条候选", flush=True)
                candidate_links = filtered

            if not candidate_links:
                # 兜底：直接从页面源码里抓取 ada 链接
                direct_matches = re.findall(
                    r'https?://ada\.baidu\.com/site/[\w.-]+(?:/xyl)?\?[^"\s<>]*imid=[\w-]+',
                    response.text or '',
                )
                # 这里 direct_matches 已经是落地页，仍按 imid 去重后再取 limit
                seen_direct_imids = set()
                direct_unique = []
                for u in list(dict.fromkeys(direct_matches)):
                    nu = canonicalize_ada_url(u)
                    imid = get_imid(nu)
                    if not imid or imid in seen_direct_imids:
                        continue
                    seen_direct_imids.add(imid)
                    direct_unique.append(nu)
                    if len(direct_unique) >= limit:
                        break
                candidate_links = direct_unique

            if not candidate_links:
                page_title = ''
                m_title = re.search(r'<title>(.*?)</title>', response.text or '', re.IGNORECASE | re.DOTALL)
                if m_title:
                    page_title = m_title.group(1).strip().replace('\n', ' ')
                print(f"关键字: {keyword} \t候选链接为空，title={page_title}")
                continue

            # 第二步：逐个解析真实落地页（按 imid 去重；重复的不要占用 limit 配额）
            seen_page_imids = set()
            resolved_cache = {}

            for candidate in candidate_links:
                if stop_event.is_set():
                    break

                # 如果已经凑够唯一广告数量，则停止继续解析
                if len(seen_page_imids) >= limit:
                    break

                # 解析每个候选之间也加一点随机延迟
                jitter_sleep(cfg.get('delay_between_resolves_range'))

                # 已经是目标链接则直接使用
                if 'ada.baidu.com' in candidate and 'imid=' in candidate:
                    match_url = candidate
                else:
                    # 先查缓存，避免重复解析
                    if candidate in resolved_cache:
                        match_url = resolved_cache[candidate]
                    else:
                        match_url = resolve_baidu_link(
                            candidate,
                            stop_event,
                            session=s,
                            user_agent=ua,
                            timeout=timeout,
                        )
                        resolved_cache[candidate] = match_url

                if not match_url:
                    continue

                normalized = canonicalize_ada_url(match_url)
                imid = get_imid(normalized)
                if not imid:
                    continue

                # 同页内同一广告（同 imid）只算一次，不消耗 limit 配额
                if imid in seen_page_imids:
                    continue

                seen_page_imids.add(imid)
                results.append(normalized)

    except Exception as e:
        print('Exception - ' + str(e))
    finally:
        if results:
            # 去重但保序
            unique = []
            seen = set()
            for u in results:
                if u not in seen:
                    seen.add(u)
                    unique.append(u)
            return unique
        else:
            print(f"关键字: {keyword} \t 未查询到匹配结果")


def scrape_ada():
    keywords = load_keywords()

    # 路径与配置
    base_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(base_dir)
    cfg = load_scrape_config(project_root)

    # 断点续传：已完成 keyword 集合
    done_keywords = set()
    resume_file = str(cfg.get('resume_file') or '').strip()
    if cfg.get('resume_enabled') and resume_file:
        os.makedirs(os.path.dirname(resume_file), exist_ok=True)
        if os.path.exists(resume_file):
            try:
                with open(resume_file, 'r', encoding='utf-8') as rf:
                    done_keywords = set(line.strip() for line in rf if line.strip())
            except Exception:
                done_keywords = set()

    pending_keywords = [k for k in keywords if k and k not in done_keywords]
    print(
        f"配置: workers={cfg.get('max_workers')} max_page={cfg.get('max_page')} limit={cfg.get('candidate_links_limit')} "
        f"resume={cfg.get('resume_enabled')} pending={len(pending_keywords)}/{len(keywords)} | resume_file={resume_file}",
        flush=True,
    )

    api_path = str(cfg.get('api_file') or os.path.join(project_root, 'api.txt'))

    # 读取现有的 api.txt，构建 imid 集合
    existing_imids = set()
    try:
        with open(api_path, 'r', encoding='utf-8') as f:
            for line in f:
                url = line.strip()
                if url:
                    imid = get_imid(url)
                    if imid:
                        existing_imids.add(imid)
        print(f"已加载 {len(existing_imids)} 个现有imid | api_path={api_path}", flush=True)
    except FileNotFoundError:
        print(f"api.txt 不存在，将创建新文件 | api_path={api_path}", flush=True)

    with open(api_path, 'a+', encoding='utf-8') as f:
        stop_event = threading.Event()

        max_workers = int(cfg.get('max_workers', 1) or 1)
        max_workers = max(1, min(8, max_workers))

        # 关键：不要一次性提交所有 keyword（否则命中风控后，已提交的任务还会继续跑，表现为“还在傻傻访问”）
        # 改为“滚动提交”：最多只保持 max_workers 个 in-flight 任务。
        executor = ThreadPoolExecutor(max_workers=max_workers)
        futures = {}  # future -> keyword
        kw_iter = iter(pending_keywords)

        def _submit_next():
            if stop_event.is_set():
                return False
            try:
                kw_ = next(kw_iter)
            except StopIteration:
                return False
            futures[executor.submit(fetch, kw_, stop_event, cfg)] = kw_
            return True

        try:
            # 先提交一批
            for _ in range(max_workers):
                if not _submit_next():
                    break

            while futures:
                # 非阻塞按键检测：按 q 请求停止
                if msvcrt is not None and msvcrt.kbhit():
                    key = msvcrt.getwch()
                    if str(key).lower() == 'q':
                        stop_event.set()

                if stop_event.is_set():
                    print('检测到全局停止信号，正在取消剩余任务...', flush=True)
                    break

                done, _ = wait(list(futures.keys()), return_when=FIRST_COMPLETED)
                for future in done:
                    kw = futures.pop(future, '') or ''

                    try:
                        result = future.result()
                    except Exception as e:
                        print(f"任务异常: kw={kw} err={e}", flush=True)
                        # 异常也继续滚动提交
                        _submit_next()
                        continue

                    # 只要任务完成（无论是否抓到新链接），都算该 keyword 已处理，用于断点续传
                    if cfg.get('resume_enabled') and resume_file and kw:
                        try:
                            with open(resume_file, 'a', encoding='utf-8') as rf:
                                rf.write(kw + '\n')
                        except Exception:
                            pass

                    if result:
                        # 主线程去重并写入，保证写文件和 existing_imids 状态一致
                        new_urls = []
                        dup_count = 0
                        for url in result:
                            imid = get_imid(url)
                            if not imid:
                                continue
                            if imid in existing_imids:
                                dup_count += 1
                                continue
                            new_urls.append(url)
                            existing_imids.add(imid)

                        if new_urls:
                            print(f"关键字：{kw} 成功提取 {len(new_urls)} 条新url（重复 {dup_count} 条）: {'  '.join(new_urls)}", flush=True)
                            f.write('\n'.join(new_urls) + '\n')
                            f.flush()  # 确保及时写入
                        else:
                            print(f"关键字：{kw} 抓取到 {len(result)} 条但全部重复（或无 imid），未写入", flush=True)
                    else:
                        print(f"关键字：{kw} 无新增", flush=True)

                    # 继续滚动提交
                    _submit_next()

        except KeyboardInterrupt:
            print('\n检测到 Ctrl+C，正在停止抓取...')
            stop_event.set()
            print('已请求停止，将保存当前已抓取结果并退出。')
        finally:
            # 取消未开始的任务；已在跑的任务无法立刻中断，但由于滚动提交，最多只有 max_workers 个在跑。
            for future in list(futures.keys()):
                future.cancel()
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                executor.shutdown(wait=False)

        # 最后再做一次全量去重和清理，保持文件整洁
        f.seek(0)
        all_lines = f.readlines()
        f.seek(0)
        f.truncate()
        
        unique_lines = []
        seen_imids = set()
        for line in all_lines:
            url = line.strip()
            if url:
                imid = get_imid(url)
                if imid:
                    if imid not in seen_imids:
                        unique_lines.append(url)
                        seen_imids.add(imid)
                else:
                    # 如果没有imid，保留原样（或者根据需求删除）
                    # 假设所有有效链接都有imid，这里保留以防万一
                    if url not in unique_lines:
                         unique_lines.append(url)

        f.write('\n'.join(unique_lines) + '\n')
        print(f'完成 api.txt 去重更新，当前共 {len(unique_lines)} 条链接')


if __name__ == "__main__":
    scrape_ada()
