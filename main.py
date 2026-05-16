import time, random, yaml, requests, logging, re, os, signal, threading
from datetime import datetime
from collections import Counter
from urllib.parse import urlparse, parse_qs
from fake_useragent import UserAgent
from concurrent.futures import ThreadPoolExecutor
from DrissionPage import ChromiumPage, ChromiumOptions

try:
    import msvcrt  # Windows: 用于无回车按键检测（按 q 退出）
except Exception:
    msvcrt = None

# 配置日志记录
LOG_DIR = 'logs'
os.makedirs(LOG_DIR, exist_ok=True)
log_date = datetime.now().strftime('%Y%m%d')
log_file = os.path.join(LOG_DIR, f'contactPutianHospital_{log_date}.log')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%y-%m-%d %H:%M:%S',
    handlers=[logging.FileHandler(log_file, encoding='utf-8'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# 全局停止标记：用于 Ctrl+C / 自定义按键退出
STOP_EVENT = threading.Event()

def _handle_sigint(sig, frame):
    """在 Windows/VSCode 终端中接收 Ctrl+C（SIGINT）。

    说明：
    - 仅设置 STOP_EVENT 往往无法打断 requests/浏览器等阻塞调用，表现为“怎么按都停不掉”。
    - 这里显式抛出 KeyboardInterrupt，让主线程尽快中断。
    """
    STOP_EVENT.set()
    raise KeyboardInterrupt

try:
    signal.signal(signal.SIGINT, _handle_sigint)
except Exception:
    # 某些运行环境可能不允许注册信号处理器
    pass

# 读取配置文件
with open('config.yaml', 'r', encoding='utf-8') as config_file:
    config = yaml.safe_load(config_file)

# 关闭单例标签页对象模式

# http://g1879.gitee.io/drissionpagedocs/ChromiumPage/browser_options/
co = (ChromiumOptions()
.set_no_imgs(config['browser']['chrome_options']['no_imgs'])  # 加载图片
.set_headless(config['browser']['chrome_options']['headless'])  # 有界面模式
.auto_port(config['browser']['chrome_options']['auto_port'])  # 自动获取端口
# .set_proxy("xxxxx")
.set_user_agent(UserAgent().random)  # 随机UserAgent
.set_paths(browser_path=config['browser']['chrome_options']['browser_path']))  # 修正浏览器路径设置方法

BAIDU_URL = config['browser']['baidu_url']
TEL_NUMBER = config['browser']['tel_number']  # 手机号码
TEL_NAME = config['browser']['tel_name']  # 名字(可选)
ENABLE_OTP = config['browser']['enable_otp']  # 是否启用验证码功能

titles = config['interaction_templates']['titles'] # 定义医生和护士的称谓
relatives = config['interaction_templates']['relatives'] # 定义亲属的称谓
situations = config['interaction_templates']['situations'] # 定义更丰富的不同情况描述
contact_methods = config['interaction_templates']['contact_methods'] # 更丰富的联系方式描述
greetings = config['interaction_templates']['greetings'] # 更丰富的打招呼方式

DEDUP_CONFIG = config.get('deduplication', {})
ENABLE_LINK_DEDUP = DEDUP_CONFIG.get('enable_link_dedup', True)
ENABLE_INSTITUTION_DEDUP = DEDUP_CONFIG.get('enable_institution_dedup', False)
ENABLE_TITLE_DEDUP = DEDUP_CONFIG.get('enable_title_dedup', True)
SHOW_DUPLICATE_EXAMPLES = max(0, int(DEDUP_CONFIG.get('show_duplicate_examples', 3)))

def extract_institution_key(url: str) -> str:
    """
    提取机构去重键，优先使用 imid，其次使用 ada 机构短码（/site/<org>/）
    """
    clean_url = (url or '').strip()
    if not clean_url:
        return ''

    # 1. 优先尝试提取 imid
    try:
        parsed = urlparse(clean_url)
        qs = parse_qs(parsed.query)
        imid_list = qs.get('imid')
        if imid_list and imid_list[0]:
            return f"imid:{imid_list[0]}"
    except Exception:
        pass

    # 2. 其次尝试提取 ada 站点 ID
    match = re.search(r'ada\.baidu\.com/site/([^/\s]+)/', clean_url, re.IGNORECASE)
    if match:
        return f"ada_site:{match.group(1).lower()}"

    # 3. 最后降级到用域名/路径
    parsed = urlparse(clean_url)
    host = (parsed.netloc or '').lower()
    first_segment = ''
    if parsed.path:
        parts = [p for p in parsed.path.strip('/').split('/') if p]
        if parts:
            first_segment = parts[0].lower()

    return f"{host}/{first_segment}" if first_segment else host

def prepare_target_urls(raw_urls):
    """
    清洗并去重 URL，输出用户友好的去重统计日志
    """
    cleaned_urls = [line.strip() for line in raw_urls if line and line.strip()]
    total_raw_count = len(cleaned_urls)

    duplicate_link_count = 0
    duplicate_institution_count = 0
    duplicate_examples = []

    urls_after_link_dedup = []
    seen_links = set()

    if ENABLE_LINK_DEDUP:
        for url in cleaned_urls:
            if url in seen_links:
                duplicate_link_count += 1
                if len(duplicate_examples) < SHOW_DUPLICATE_EXAMPLES:
                    duplicate_examples.append(f"[重复链接] {url}")
                continue
            seen_links.add(url)
            urls_after_link_dedup.append(url)
    else:
        urls_after_link_dedup = cleaned_urls

    final_urls = []
    if ENABLE_INSTITUTION_DEDUP:
        seen_institutions = {}
        for url in urls_after_link_dedup:
            institution_key = extract_institution_key(url)
            key = institution_key or url
            if key in seen_institutions:
                duplicate_institution_count += 1
                if len(duplicate_examples) < SHOW_DUPLICATE_EXAMPLES:
                    duplicate_examples.append(
                        f"[重复机构] {key} | 保留: {seen_institutions[key]} | 跳过: {url}"
                    )
                continue
            seen_institutions[key] = url
            final_urls.append(url)
    else:
        final_urls = urls_after_link_dedup

    logger.info(
        "链接预处理完成：原始 %s 条，去重后 %s 条（重复链接 %s 条，重复机构 %s 条）",
        total_raw_count,
        len(final_urls),
        duplicate_link_count,
        duplicate_institution_count,
    )

    for example in duplicate_examples:
        logger.info("去重示例：%s", example)

    return final_urls

def generate_ai_message(config, full_context):
    """
    使用自定义 AI API 生成对话消息
    :param config: 配置字典
    :param context: 标题和上下文的字典
    :return: 生成的对话消息字符串
    """
    try:
        if STOP_EVENT.is_set():
            return None

        hospital_name = full_context.get('title', '')
        context_str = full_context.get('context', '')

        # 准备 API 请求参数
        ai_config = config['dialogue_mode']['ai_config']
        content = ai_config['prompt_template'].format(
            tel_number=TEL_NUMBER,
            tel_name=TEL_NAME,
            hospital_name=hospital_name,
            chat_context=context_str
        )
        payload = {
            "model": ai_config['model'],
            "messages": [{"role": "user", "content": content}],
            **ai_config['request_params']
        }

        # 发送 API 请求（增加连接与读取超时，避免长时间阻塞）
        response = requests.post(
            f"{ai_config['base_url']}/chat/completions",
            headers={
                "Authorization": f"Bearer {ai_config['api_key']}",
                "Content-Type": "application/json"
            },
            json=payload,
            proxies={"http": "", "https": ""},
            # Ctrl+C 更容易生效；同时避免网络卡住时看起来“无法终止”
            timeout=(5, 45)
        )
        response.raise_for_status()

        # 解析响应并做完整性校验
        result = response.json()
        choices = result.get('choices') or []
        if not choices:
            raise ValueError(f"AI 响应缺少 choices: {result}")

        choice = choices[0] or {}
        message = choice.get('message') or {}
        raw_content = message.get('content', '')
        finish_reason = choice.get('finish_reason')

        if isinstance(raw_content, list):
            generated_message = ''.join(
                part.get('text', '') if isinstance(part, dict) else str(part)
                for part in raw_content
            ).strip()
        else:
            generated_message = str(raw_content).strip()

        if not generated_message:
            raise ValueError(f"AI 响应 content 为空，finish_reason={finish_reason}")

        # 兼容不同网关/模型的结束标记
        # 说明：部分 OpenAI 兼容网关在文本已可用时也会返回 finish_reason=length
        # 因此不直接判失败，先做可用性兜底判断
        valid_finish_reason = {None, 'stop', 'end_turn', 'length'}
        if finish_reason not in valid_finish_reason:
            raise ValueError(f"AI 响应结束状态异常，finish_reason={finish_reason}")

        if finish_reason == 'length':
            logger.warning(f"AI 响应触发长度截止，已降级按可用文本返回: finish_reason={finish_reason}，{generated_message}")
            # 如遇半句，尽量截到最近一个中文句末标点，避免发出明显残句
            punctuations = ('。', '！', '？', '.', '!', '?')
            last_punc_idx = max(generated_message.rfind(p) for p in punctuations)
            if last_punc_idx > 0:
                generated_message = generated_message[:last_punc_idx + 1].strip()

            # 若仍过短，视作无效，交由模板模式兜底
            if len(generated_message) < 10:
                raise ValueError("AI 响应长度截止且内容过短，判定为不完整")

        return generated_message

    except requests.exceptions.Timeout as e:
        logger.error(f"AI 对话请求超时: {e}")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"AI 对话请求失败: {e}")
        return None
    except (ValueError, KeyError, TypeError) as e:
        logger.error(f"AI 响应解析/校验失败: {e}")
        return None
    except Exception as e:
        logger.error(f"AI 对话生成失败: {e}")
        return None

def generate_message(config, full_context):
    """
    根据配置的对话模式生成消息
    :param config: 配置字典
    :param context: 含机构标题和可能的上下文信息
    :return: 生成的消息字符串
    """
    dialogue_mode = config['dialogue_mode']['mode']
    if dialogue_mode == 'ai' or (dialogue_mode == 'hybrid' and random.random() < 0.5):
        ai_message = generate_ai_message(config, full_context)
        if ai_message: return ai_message
    # 默认或降级到模板模式
    title = random.choice(titles)            # 随机选择一条打招呼用语模板
    relative = random.choice(relatives)
    situation = random.choice(situations)
    contact_method_template = random.choice(contact_methods)
    contact_method = contact_method_template.replace("{number}", TEL_NUMBER) # 替换模板中的 {number} 为实际的电话号码 TEL_NUMBER
    greeting = random.choice(greetings)            # 随机选择一个打招呼方式
    
    return f"{greeting}{title}，{relative}{TEL_NAME}{situation}，{contact_method}。"

def process_tab(page:ChromiumPage, url:str, success_counter:Counter, total_len):
    """
    处理网页标签函数，增加上下文检测逻辑
    :param page: ChromiumPage对象，用于管理和操作浏览器标签页
    :param url: 网页地址
    :param success_counter: 计数器对象
    :param total_len: 总URL数量
    """
    tab = None
    tab_title = None
    sent_message = False
    try:
        if STOP_EVENT.is_set():
            return
        # tid = page.new_tab(url)
        # tab = page.get_tab(tid)
        url = url.strip()
        tid = page.new_tab(url)
        tab = page.get_tab(tid)
        tab.wait.load_start(timeout=5)
        if STOP_EVENT.is_set():
            return
        tab_title = tab.title

        # 检测机构聊天界面上下文
        # 使用合并的CSS选择器，避免顺序查找导致的超时等待
        time.sleep(1)
        context_element = tab.ele('css:div.pc-component-chatview-wrapper, div.component-chatview-wrapper, ' \
                                  'div.msg-area-container, div.gt-merchant-bot-welcome-area-root-container', timeout=3)

        context = ""
        if context_element:
            # 显式转换为字符串以避免静态分析错误
            context = str(context_element.text).strip()

        logger.debug(context)
        # 将上下文信息整合
        full_context = {'title': tab_title, 'context': context}
        # full_context = f"{tab_title}\n上下文：{context}" if context else tab_title

        # 合并输入框选择器
        component_input = tab.ele(
            'css:.imlp-component-newtypebox-textarea, .imlp-component-typebox-input, '
            'textarea.bot-pc-text-input-textarea',
            timeout=10
        )

        # 兼容部分机构 H5 聊天页：先展示 fake-input，点击后才渲染真实 textarea
        if not component_input:
            fake_input = tab.ele('css:div.fake-input', timeout=2)
            if fake_input:
                try:
                    fake_input.click()
                except Exception as e:
                    logger.warning('点击 fake-input 失败，尝试 JS 点击 | 标题:%s | url:%s | err:%s', tab_title, url, e)
                    try:
                        fake_input.run_js('this.click()')
                    except Exception as e2:
                        logger.warning('JS 点击 fake-input 也失败 | 标题:%s | url:%s | err:%s', tab_title, url, e2)

                time.sleep(0.5)
                component_input = tab.ele(
                    'css:div.text-input textarea, textarea.bot-pc-text-input-textarea, textarea',
                    timeout=3
                )

        if not component_input:
            logger.info('跳过：未找到输入框 | 标题:%s | url:%s', tab_title, url)
            return
            
        component_input.clear()

        if ENABLE_TITLE_DEDUP and tab_title and success_counter[tab_title] > 0:
            logger.info('跳过：命中重复标题:%s | 已留言次数:%s | url:%s', tab_title, success_counter[tab_title], url)
            return

        # 使用消息生成函数
        start_gen_time = time.time()
        template = generate_message(config, full_context)
        logger.info(f"生成消息耗时: {time.time() - start_gen_time:.2f}s\n{template}")
        if STOP_EVENT.is_set():
            return

        component_input.input(template)
        component_input.input('\n')  # 按Enter发送消息

        # 合并发送按钮选择器
        send = tab.ele(
            'css:.imlp-component-newtypebox-send, .imlp-component-typebox-send-btn, '
            'div.send-btn, div.icon.send-btn',
            timeout=5
        )

        if not send:
            logger.warning('未找到发送按钮，无法发送 | 标题:%s | url:%s', tab_title, url)
            return
        try:
            # 优先尝试普通点击，如果失败则回退
            send.click()
            sent_message = True
        except Exception as e:
            logger.warning(f"点击发送按钮失败，尝试JS点击: {e}")
            try:
                send.run_js('this.click()')
                sent_message = True
            except Exception as e2:
                logger.error(f"JS点击也失败: {e2}")
        time.sleep(2)  # 等待发送完成

    except Exception as e:
        logger.error(f"发生错误: {e}")
    finally:
        if tab:
            time.sleep(2)  # 增加延迟，确保消息发送成功
            page.close_tabs(tabs_or_ids=tab)
            if tab_title and sent_message:
                success_counter.update([tab_title])
                logger.info(f"已留言, {len(success_counter)}/{total_len}, 标题:{tab_title}, url: {url}\n")

def iterate_api(file_path):
    """
    迭代处理API函数
    - 创建 ChromiumPage 对象（page）用于管理和操作浏览器标签页
    - 打开百度页面并等待加载开始
    - 读取文件中的所有 URL，并计算总长度（total_len）
    - 初始化成功计数器 success_count
    - 如果开启ENABLE_OTP则循环遍历执行 process_tab 函数，否则使用线程池并发执行 process_tab 函数
    :param file_path: API文件路径
    """

    # 尝试接管已启动的浏览器（端口9222），若失败则启动新浏览器实例
    try:
        page = ChromiumPage(addr_driver_opts='127.0.0.1:9222')
        # 简单验证连接状态（获取当前URL或标题，若未连接成功通常会抛出异常）
        _ = page.title
        logger.info("成功接管已启动的浏览器 (127.0.0.1:9222)")
    except Exception as e:
        logger.info(f"未检测到已启动的浏览器 (127.0.0.1:9222)，正在启动新实例... ({e})")
        page = ChromiumPage(addr_driver_opts=co)

    try:
        page.get(BAIDU_URL)
        page.wait.load_start(timeout=5)
        with open(file_path, 'r', encoding='utf-8') as file:
            raw_urls = file.readlines()
    except KeyboardInterrupt:
        STOP_EVENT.set()
        logger.info("启动阶段检测到中断（Ctrl+C）")
        raise

    urls = prepare_target_urls(raw_urls)
    if len(urls) == 0:
        logger.warning('未读取到可用链接，流程结束')
        return

    # 读取已访问的URL记录
    visited_file = 'visited_urls.txt'
    visited_urls = set()
    if os.path.exists(visited_file):
        with open(visited_file, 'r', encoding='utf-8') as f:
            visited_urls = set(line.strip() for line in f if line.strip())

    # 过滤掉已访问的URL
    remaining_urls = [url for url in urls if url not in visited_urls]
    logger.info(f"总链接数: {len(urls)}, 已访问: {len(visited_urls)}, 剩余: {len(remaining_urls)}")

    if not remaining_urls:
        logger.info("所有链接已处理完毕，重置访问记录...")
        # 这里选择清空记录，以便下次重新开始
        if os.path.exists(visited_file):
            os.remove(visited_file)
        visited_urls.clear()
        remaining_urls = urls

    # 保留随机性：只对“剩余链接”随机打乱
    random.shuffle(remaining_urls)
    total_len = len(remaining_urls)
    success_counter = Counter()

    logger.info("运行中可在终端按 Ctrl+C 或按键 'q' 请求停止（通常在当前链接处理结束后生效）\n")

    # 断点续传策略：在开始处理某个 url 前就写入 visited_file，确保中断后不重复
    # 如果您希望“只有真正发送成功才算访问过”，可把写入逻辑移动到 process_tab 成功后。
    try:
        with open(visited_file, 'a', encoding='utf-8') as vf:
            for url in remaining_urls:
                # 非阻塞按键检测：按 q 立即请求停止
                if msvcrt is not None and msvcrt.kbhit():
                    key = msvcrt.getwch()
                    if str(key).lower() == 'q':
                        STOP_EVENT.set()

                if STOP_EVENT.is_set():
                    logger.info("收到停止请求，将在安全点退出")
                    break

                url = (url or '').strip()
                if not url:
                    continue

                if url not in visited_urls:
                    vf.write(url + '\n')
                    vf.flush()
                    visited_urls.add(url)

                process_tab(page, url, success_counter, total_len)

    except KeyboardInterrupt:
        STOP_EVENT.set()
        logger.info("检测到中断（Ctrl+C），已保存进度")
        raise  # 重新抛出异常以便外层捕获

if __name__ == '__main__':
    if TEL_NUMBER.isdigit():
        try:
            start_time = time.time()
            iterate_api('api.txt')
            end_time = time.time()
            logger.info(f"结束！总耗时: {end_time - start_time} seconds")
        except KeyboardInterrupt:
            logger.info("用户手动终止程序")
    else:
        logger.error("请先设置手机号码")
