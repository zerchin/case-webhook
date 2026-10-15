import os
import logging
import json
import re
import hashlib
import hmac
import ipaddress
import pymysql
import requests
from flask import Flask, request, jsonify
from datetime import datetime
import time

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

WEBHOOK_PATH_PATTERN = re.compile(r'^/webhooks/pd/v3/[0-9a-f]{64}$')


def load_webhook_path():
    """从环境变量读取并校验 Webhook 路径，配置错误时拒绝启动。"""
    webhook_path = os.getenv('WEBHOOK_PATH', '')
    if not WEBHOOK_PATH_PATTERN.fullmatch(webhook_path):
        raise RuntimeError(
            'WEBHOOK_PATH must use /webhooks/pd/v3/<64 lowercase hex characters>'
        )
    return webhook_path


WEBHOOK_PATH = load_webhook_path()
app = Flask(__name__)

SENSITIVE_HEADERS = {
    'authorization',
    'cookie',
    'proxy-authorization',
    'set-cookie',
    'x-api-key',
    'x-pagerduty-signature',
}

class DatabaseManager:
    def __init__(self):
        self.host = os.getenv('MYSQL_HOST', '192.168.2.65')
        self.user = os.getenv('MYSQL_USER', 'root')
        self.password = os.getenv('MYSQL_PASSWORD', '123456')
        self.database = os.getenv('MYSQL_DB', 'case')
        
    def get_connection(self):
        return pymysql.connect(
            host=self.host,
            user=self.user,
            password=self.password,
            database=self.database,
            charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor
        )

    def case_exists(self, case_id):
        """检查case是否已存在"""
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cursor:
                    sql = "SELECT id FROM cases WHERE id = %s"
                    cursor.execute(sql, (case_id,))
                    return cursor.fetchone() is not None
        except Exception as e:
            logger.error(f"检查case存在性时出错: {e}")
            return False

    def get_support_with_priority_gt_100(self):
        """获取 priority 大于 100 的 support"""
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cursor:
                    sql = """
                    SELECT *
                    FROM support_list 
                    WHERE priority > 100 AND status = 'online' 
                    ORDER BY updated_at ASC 
                    LIMIT 1

                    """
                    cursor.execute(sql)
                    return cursor.fetchone()
        except Exception as e:
            logger.error(f"获取 priority 大于 100 的 support 时出错: {e}")
            return False 


    def get_oldest_online_support(self):
        """获取updated_at最早的在线support"""
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cursor:
                    sql = """
                    SELECT *
                    FROM support_list 
                    WHERE status = 'online' 
                    ORDER BY updated_at ASC 
                    LIMIT 1
                    """
                    cursor.execute(sql)
                    return cursor.fetchone()
        except Exception as e:
            logger.error(f"获取support时出错: {e}")
            return None

    def update_support_priority(self, support_name, delta):
        """更新 support priority"""
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cursor:
                    # 更新support的priority
                    sql_support = """
                    UPDATE support_list 
                    SET priority = priority + %s
                    WHERE name = %s
                    """
                    cursor.execute(sql_support, (delta, support_name))
                    
                    conn.commit()
                    logger.info(f"成功更新 support {support_name} priority 变化 {delta}")
                    return True
        except Exception as e:
            logger.error(f"插入case时出错: {e}")
            return False       
 
    def update_support_updated_at(self, support_name):
        """更新 support updated_at"""
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cursor:
                    # 更新support的updated_at
                    sql_support = """
                    UPDATE support_list 
                    SET updated_at = NOW()
                    WHERE name = %s
                    """
                    cursor.execute(sql_support, (support_name ))
                    
                    conn.commit()
                    logger.info(f"成功更新 support {support_name} updated_at to now ")
                    return True
        except Exception as e:
            logger.error(f"更新 support 的 updated_at 时出错: {e}")
            return False  

    def insert_case(self, case_id, support_name, support_id):
        """插入新的case记录"""
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cursor:
                    # 插入case
                    sql_case = """
                    INSERT INTO cases (id, support_name, support_id, created_at) 
                    VALUES (%s, %s, %s, NOW())
                    """
                    cursor.execute(sql_case, (case_id, support_name, support_id))
                    
                    conn.commit()
                    logger.info(f"成功插入case {case_id} ")
                    return True
        except Exception as e:
            logger.error(f"插入case时出错: {e}")
            return False

class SlackNotifier:
    def __init__(self):
        self.webhook_url = os.getenv('SLACK_Webhook_URL')
    
    def send_message(self, message, max_retries=15):
        """发送消息到Slack，带重试机制"""
        if not self.webhook_url:
            logger.error("SLACK_Webhook_URL 未配置")
            return False
        
        for attempt in range(max_retries):
            try:
                response = requests.post(
                    self.webhook_url,
                    json={'message': message},
                    headers={'Content-Type': 'application/json'},
                    timeout=10
                )
                
                if response.status_code == 200:
                    logger.info(f"Slack消息发送成功 (第{attempt + 1}次尝试)")
                    return True
                else:
                    logger.warning(f"Slack返回错误状态码: {response.status_code} (第{attempt + 1}次尝试)")
            
            except requests.exceptions.RequestException as e:
                logger.warning(f"发送Slack消息失败: {e} (第{attempt + 1}次尝试)")
            
            # 如果不是最后一次尝试，等待后重试
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # 指数退避
        
        logger.error(f"Slack消息发送失败，已重试{max_retries}次")
        return False

def extract_case_id(title):
    """从title中提取case id"""
    # 匹配类似 "Case 01590054 - Medium - Customer‘s Company" 的格式
    match = re.search(r'Case\s+(\d+)', title)
    if match:
        return match.group(1)
    return None

def extract_field(data, field):
    val = data.get('event', {}).get('data', {}).get(field, '')
    logger.info(f"提取到{field}: {val}")
    return val


def verify_pagerduty_signature(payload, signature_header, secret):
    """验证 PagerDuty V3 Webhook 的 HMAC-SHA256 签名。"""
    if not payload or not signature_header or not secret:
        return False

    expected_signature = "v1=" + hmac.new(
        secret.encode('utf-8'),
        payload,
        hashlib.sha256
    ).hexdigest()

    # PagerDuty 在轮换 secret 时可能会发送多个逗号分隔的签名。
    signatures = (signature.strip() for signature in signature_header.split(','))
    return any(
        hmac.compare_digest(expected_signature, signature)
        for signature in signatures
        if signature.startswith('v1=')
    )


def get_safe_request_headers(headers):
    """返回用于日志的 Header，保留所有名称并脱敏认证信息。"""
    return {
        name: '[REDACTED]' if name.lower() in SENSITIVE_HEADERS else value
        for name, value in headers.items()
    }


def log_webhook_request(raw_payload):
    """以单行 JSON 记录 Webhook 请求详情，避免 Body 中的换行伪造日志条目。"""
    request_details = {
        'remote_ip': request.remote_addr,
        'method': request.method,
        'path': request.path,
        'headers': get_safe_request_headers(request.headers),
        'body': raw_payload.decode('utf-8', errors='replace'),
    }
    logger.info(
        "Webhook 请求详情: %s",
        json.dumps(request_details, ensure_ascii=False, separators=(',', ':'))
    )


def is_supplement_request(data):
    """仅将布尔 true 或字符串 true 识别为 supplement 请求。"""
    supplement = data.get('event', {}).get('data', {}).get('supplement', False)
    return supplement is True or (
        isinstance(supplement, str) and supplement.lower() == 'true'
    )


def is_trusted_supplement_source(remote_ip, trusted_ips):
    """检查 supplement 请求来源 IP 是否在配置的 IP/CIDR 白名单中。"""
    if not remote_ip or not trusted_ips:
        return False

    try:
        source_ip = ipaddress.ip_address(remote_ip)
    except ValueError:
        logger.warning("Supplement 请求的连接端 IP 无效: %s", remote_ip)
        return False

    for entry in trusted_ips.split(','):
        entry = entry.strip()
        if not entry:
            continue
        try:
            if source_ip in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            logger.error("SUPPLEMENT_TRUSTED_IPS 包含无效配置: %s", entry)

    return False


@app.route(WEBHOOK_PATH, methods=['POST'])
def webhook_receiver():
    """Webhook接收器"""
    try:
        # 记录接收到的请求
        logger.info("收到Webhook请求")

        # 先读取未经解析或重新编码的原始请求体，供日志和签名校验共用。
        raw_payload = request.get_data(cache=True)
        log_webhook_request(raw_payload)

        data = request.get_json(silent=True)
        if not data:
            logger.error("无法解析JSON数据，请求 IP: %s", request.remote_addr)
            return jsonify({'error': 'Invalid JSON'}), 400

        pagerduty_secret = os.getenv('PAGERDUTY_WEBHOOK_SECRET')
        signature_header = request.headers.get('X-PagerDuty-Signature', '')
        has_valid_pagerduty_signature = verify_pagerduty_signature(
            raw_payload,
            signature_header,
            pagerduty_secret
        )

        if is_supplement_request(data):
            trusted_ips = os.getenv('SUPPLEMENT_TRUSTED_IPS', '')
            if has_valid_pagerduty_signature:
                logger.info("Supplement 请求已通过 PagerDuty 签名校验")
            elif is_trusted_supplement_source(request.remote_addr, trusted_ips):
                logger.info(
                    "Supplement 请求已通过来源 IP 白名单校验: %s",
                    request.remote_addr
                )
            else:
                logger.warning(
                    "拒绝未授权的 Supplement 请求，请求 IP: %s",
                    request.remote_addr
                )
                return jsonify({'error': 'Untrusted supplement source'}), 403
        else:
            if not pagerduty_secret:
                logger.critical("PAGERDUTY_WEBHOOK_SECRET 未配置，已拒绝 PagerDuty Webhook 请求")
                return jsonify({'error': 'Webhook signature verification is not configured'}), 503

            if not signature_header:
                logger.warning(
                    "拒绝缺少 X-PagerDuty-Signature 的 Webhook 请求，请求 IP: %s",
                    request.remote_addr
                )
                return jsonify({'error': 'Missing webhook signature'}), 400

            if not has_valid_pagerduty_signature:
                logger.warning(
                    "拒绝 PagerDuty 签名无效的 Webhook 请求，请求 IP: %s",
                    request.remote_addr
                )
                return jsonify({'error': 'Invalid webhook signature'}), 403


        # 判断是否 supplement
        supplement = is_supplement_request(data)
        logger.info("提取到supplement: %s", supplement)
        if not supplement:
            logger.info(f"获取到 PagerDuty 请求")

            # 提取title
            title = extract_field(data, 'title')

            # 提取case id
            case_id = extract_case_id(title)
            if not case_id:
                logger.error(f"无法从title中提取case id: {title}")
                return jsonify({'error': 'Cannot extract case id from title'}), 400
            
            logger.info(f"提取到case id: {case_id}")
            
            # 初始化数据库管理器
            db = DatabaseManager()
            
            # 检查case是否已存在
            if db.case_exists(case_id):
                logger.info(f"case {case_id} 已存在，跳过处理")
                return jsonify({'status': 'skipped', 'reason': 'case already exists'}), 200
            

            # 判断是否有大于 100 priority 的 support
            high_priority_support = db.get_support_with_priority_gt_100()  ## 未完成的函数
            if not high_priority_support:
                # 获取最早的在线support
                support = db.get_oldest_online_support()
                if not support:
                    logger.error("没有找到在线的support")
                    return jsonify({'error': 'No online support found'}), 500

                # 如果 priority < 100 则跳过该 support 并 priority - 1，并重新获取新的 support
                while  support['priority'] < 100:
                    if not all([db.update_support_priority(support['name'], 1), db.update_support_updated_at(support['name'])]):    ## 未完成的函数
                        logger.error("support 更新 priority + 1 失败")
                        return jsonify({'error': 'Failed to update support priority'}), 500
                    support = db.get_oldest_online_support()
                
                support_name = support['name']
                support_id = support['id']
                
                logger.info(f"分配到support: {support_name} ({support_id})")
                
                # 插入新的case记录并更新support时间
                if not db.insert_case(case_id, support_name, support_id):
                    logger.error("插入case记录失败")
                    return jsonify({'error': 'Failed to insert case record'}), 500
                if not db.update_support_updated_at(support_name):
                    logger.error("更新 support 时间失败")
                    return jsonify({'error': 'Failed to update support date'}), 500
                
            else:
                support_name = high_priority_support['name']
                support_id = high_priority_support['id']

                # 插入新的case记录并更新support时间
                if not db.insert_case(case_id, support_name, support_id):
                    logger.error("插入case记录失败")
                    return jsonify({'error': 'Failed to insert case record'}), 500                
                if not db.update_support_priority(support_name, -1):
                    logger.error("support 更新 priority - 1 失败")
                    return jsonify({'error': 'Failed to update support priority'}), 500

                logger.info(f"分配到support: {support_name} ({support_id})")

            # 发送Slack通知
            slack = SlackNotifier()
            message = f"{title}\nOwner: {support_name} <@{support_id}>"



        else:
            logger.info(f"获取到 Supplement 请求")
            # 提取title
            title = extract_field(data, 'title')

            # 提取case id
            case_id = extract_field(data, 'case_id')


            # 提取new_support_name
            support_name = extract_field(data, 'new_support_name')

            # 提取new_support_id
            support_id = extract_field(data, 'new_support_id')

            # 提取old_support_name
            old_support_name = extract_field(data, 'old_support_name')

            # 初始化数据库管理器
            db = DatabaseManager()

            # 更新 old_support priority + 1
            db.update_support_priority(old_support_name, 1)

            # 更新 new_support priority - 1
            db.update_support_priority(support_name, -1)

            # 发送Slack通知
            slack = SlackNotifier()
            message = f"{title} {case_id}\nOwner Changed From {old_support_name} to {support_name} <@{support_id}>\n" 

        if slack.send_message(message):
            logger.info("Slack通知发送成功")
            return jsonify({
                'status': 'success', 
                'case_id': case_id
            }), 200
        else:
            logger.error("Slack通知发送失败")
            return jsonify({
                'status': 'partial_success',
                'message': 'Case processed but Slack notification failed',
                'case_id': case_id
            }), 207  # 207 Multi-Status


    except Exception as e:
        logger.error(f"处理Webhook时发生错误: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """健康检查端点"""
    return jsonify({'status': 'healthy'}), 200

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
