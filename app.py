import os
import logging
import json
import re
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

app = Flask(__name__)

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
    
    def get_oldest_online_support(self):
        """获取updated_at最早的在线support"""
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cursor:
                    sql = """
                    SELECT name, id, status 
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
                    
                    # 更新support的updated_at时间
                    sql_support = """
                    UPDATE support_list 
                    SET updated_at = NOW() 
                    WHERE id = %s
                    """
                    cursor.execute(sql_support, (support_id,))
                    
                    conn.commit()
                    logger.info(f"成功插入case {case_id} 并更新support {support_id}")
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
    # 匹配类似 "Case 01590054 - Medium - People Yun" 的格式
    match = re.search(r'Case\s+(\d+)', title)
    if match:
        return match.group(1)
    return None

@app.route('/5c2df3d1-3371-47bd-a9cf-1983e9adc18b', methods=['POST'])
def webhook_receiver():
    """Webhook接收器"""
    try:
        # 记录接收到的请求
        logger.info("收到Webhook请求")
        
        # 解析JSON数据
        data = request.get_json()
        if not data:
            logger.error("无法解析JSON数据")
            return jsonify({'error': 'Invalid JSON'}), 400
        
        # 提取title
        title = data.get('event', {}).get('data', {}).get('title', '')
        logger.info(f"提取到title: {title}")
        
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
        
        # 获取最早的在线support
        support = db.get_oldest_online_support()
        if not support:
            logger.error("没有找到在线的support")
            return jsonify({'error': 'No online support found'}), 500
        
        support_name = support['name']
        support_id = support['id']
        
        logger.info(f"分配到support: {support_name} ({support_id})")
        
        # 插入新的case记录并更新support时间
        if not db.insert_case(case_id, support_name, support_id):
            logger.error("插入case记录失败")
            return jsonify({'error': 'Failed to insert case record'}), 500
        
        # 发送Slack通知
        slack = SlackNotifier()
        message = f"{title}\nOwner: {support_name} <@{support_id}>"
        
        if slack.send_message(message):
            logger.info("Slack通知发送成功")
            return jsonify({
                'status': 'success', 
                'case_id': case_id,
                'support_name': support_name,
                'support_id': support_id
            }), 200
        else:
            logger.error("Slack通知发送失败")
            return jsonify({
                'status': 'partial_success',
                'message': 'Case processed but Slack notification failed',
                'case_id': case_id,
                'support_name': support_name,
                'support_id': support_id
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
