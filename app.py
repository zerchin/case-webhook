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
    # 匹配类似 "Case 01590054 - Medium - People Yun" 的格式
    match = re.search(r'Case\s+(\d+)', title)
    if match:
        return match.group(1)
    return None

def extract_field(data, field):
    val = data.get('event', {}).get('data', {}).get(field, '')
    logger.info(f"提取到{field}: {val}")
    return val


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


        # 判断是否 supplement
        supplement = extract_field(data, 'supplement')
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
