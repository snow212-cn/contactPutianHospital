# -*-coding:utf8-*-

import re
import time
import random
import requests
from fake_useragent import UserAgent
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading


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


def proxies():
    return {
        # 如需使用代理请自行替换，这里有一些不太好用的免费代理
        # - https://www.docip.net/#index
        # - https://openproxy.space/list

        # "http": "x.x.x.x:xx",
        # "https": "x.x.x.x:xx",
    }


def get_imid(url):
    """从URL中提取imid参数"""
    match = re.search(r'imid=([\w-]+)', url)
    if match:
        return match.group(1)
    return None


def extract_baidu_result_links(html):
    """从百度搜索结果页提取可点击跳转链接（按页面顺序）"""
    import html as html_lib

    decoded_html = html_lib.unescape(html or '')
    links = []
    # 百度结果页中 link 可能是绝对地址、相对地址、JSON 字段等多种形态
    patterns = [
        r'href=["\'](?P<u>https?://www\.baidu\.com/link\?url=[^"\'\s>]+)["\']',
        r'href=["\'](?P<u>/link\?url=[^"\'\s>]+)["\']',
        r'["\']url["\']\s*:\s*["\'](?P<u>https?://www\.baidu\.com/link\?url=[^"\']+)["\']',
        r'["\']url["\']\s*:\s*["\'](?P<u>/link\?url=[^"\']+)["\']',
        r'data-landurl=["\'](?P<u>https?://[^"\']+)["\']',
        r'["\']mu["\']\s*:\s*["\'](?P<u>https?://[^"\']+)["\']',
    ]

    for pattern in patterns:
        for m in re.finditer(pattern, decoded_html):
            u = m.group('u').strip()
            if u.startswith('/link?url='):
                u = 'https://www.baidu.com' + u
            links.append(u)

    # 去重但保序
    unique_links = []
    seen = set()
    for link in links:
        if link not in seen:
            seen.add(link)
            unique_links.append(link)
    return unique_links


def resolve_baidu_link(baidu_link, stop_event):
    """打开百度跳转链接，拿到真实落地页 URL"""
    if stop_event.is_set():
        return None

    try:
        resp = requests.get(
            baidu_link,
            headers=headers(),
            proxies=proxies(),
            timeout=(5, 10),
            allow_redirects=True,
        )
        final_url = (resp.url or '').strip()
        if 'ada.baidu.com' in final_url and 'imid=' in final_url:
            return final_url

        # 部分场景会在响应内容里带真实链接，做一次兜底提取
        text = resp.text or ''
        m = re.search(r'https?://ada\\.baidu\\.com/site/[\\w.-]+(?:/xyl)?\\?[^"\\s<>]*imid=[\\w-]+', text)
        if m:
            return m.group(0)
    except Exception:
        pass

    return None


def fetch(keyword, existing_imids, stop_event):
    if stop_event.is_set():
        return []

    delay = random.uniform(0.5, 2.5)
    print(f"当前关键字: {keyword} \t {delay}秒后开始提取")
    time.sleep(delay)

    if stop_event.is_set():
        return []

    max_page = 2
    # 搜索词按“关键词 + site限定”拼接，提升命中 ada 结果概率
    url_template = 'http://www.baidu.com/s?wd={}&pn={}'
    results = []
    try:
        # query = f"{keyword} site:ada.baidu.com"
        query = f"{keyword}"
        urls_to_fetch = [url_template.format(query, page * 10) for page in range(max_page)]
        for url in urls_to_fetch:
            if stop_event.is_set():
                break

            response = requests.get(url, headers=headers(), proxies=proxies(), timeout=(5, 10))
            # 第一步：先提取结果链接（含百度跳转链接、mu/data-landurl等）
            candidate_links = extract_baidu_result_links(response.text)
            # 只尝试前若干条，优先处理顶部结果
            candidate_links = candidate_links[:10]

            if not candidate_links:
                # 兜底：直接从页面源码里抓取 ada 链接
                direct_matches = re.findall(
                    r'https?://ada\.baidu\.com/site/[\w.-]+(?:/xyl)?\?[^"\s<>]*imid=[\w-]+',
                    response.text or '',
                )
                candidate_links = list(dict.fromkeys(direct_matches))[:10]

            if not candidate_links:
                page_title = ''
                m_title = re.search(r'<title>(.*?)</title>', response.text or '', re.IGNORECASE | re.DOTALL)
                if m_title:
                    page_title = m_title.group(1).strip().replace('\n', ' ')
                print(f"当前关键字: {keyword} \t 候选链接为空，可能触发反爬/验证码，title={page_title}")
                continue

            # 第二步：逐个解析真实落地页
            for candidate in candidate_links:
                if stop_event.is_set():
                    break

                # 已经是目标链接则直接使用
                if 'ada.baidu.com' in candidate and 'imid=' in candidate:
                    match_url = candidate
                else:
                    # 百度跳转链接需要打开后跟随跳转拿真实落地页
                    match_url = resolve_baidu_link(candidate, stop_event)

                if not match_url:
                    continue

                imid = get_imid(match_url)
                if imid and imid not in existing_imids:
                    results.append(match_url)
                    existing_imids.add(imid)  # 实时更新已存在的imid，避免同一次抓取中重复

    except Exception as e:
        print('Exception - ' + str(e))
    finally:
        if results:
            return list(set(results))
        else:
            print(f"当前关键字: {keyword} \t 未查询到匹配结果")


def scrape_ada():
    keywords = load_keywords()
    
    # 读取现有的api.txt，构建imid集合
    existing_imids = set()
    try:
        with open('../api.txt', 'r', encoding='utf-8') as f:
            for line in f:
                url = line.strip()
                if url:
                    imid = get_imid(url)
                    if imid:
                        existing_imids.add(imid)
        print(f"已加载 {len(existing_imids)} 个现有imid")
    except FileNotFoundError:
        print("api.txt 不存在，将创建新文件")

    with open('../api.txt', 'a+', encoding='utf-8') as f:
        stop_event = threading.Event()
        with ThreadPoolExecutor(max_workers=2) as executor:
            from functools import partial
            fetch_with_imids = partial(fetch, existing_imids=existing_imids, stop_event=stop_event)
            futures = [executor.submit(fetch_with_imids, keyword) for keyword in keywords]

            try:
                for future in as_completed(futures):
                    if stop_event.is_set():
                        break

                    result = future.result()
                    if result:
                        # 再次过滤，确保线程间没有重复（虽然概率较低）
                        new_urls = []
                        for url in result:
                            imid = get_imid(url)
                            if imid and imid not in existing_imids:
                                new_urls.append(url)
                                existing_imids.add(imid)

                        if new_urls:
                            print(f"成功提取{len(new_urls)}条新url: {'  '.join(new_urls)}")
                            f.write('\n'.join(new_urls) + '\n')
                            f.flush()  # 确保及时写入
            except KeyboardInterrupt:
                print('\n检测到 Ctrl+C，正在停止抓取...')
                stop_event.set()
                for future in futures:
                    future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                print('已请求停止，将保存当前已抓取结果并退出。')

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
