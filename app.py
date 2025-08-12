from flask import Flask, request, jsonify
import requests
import pymysql
from datetime import datetime
import os
import logging
import json

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('WebhookReceiver')

app = Flask(__name__)

# 从环境变量获取配置
SLACK_WEBHOOK_URL = os.getenv('SLACK_WEBHOOK_URL')
PORT = int(os.getenv('PORT', '5000'))  # 默认端口 5000

# MySQL 数据库配置从环境变量获取
DB_CONFIG = {
    'host': os.getenv('DB_HOST'),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASSWORD'),
    'database': os.getenv('DB_DATABASE', 'case_system'),
    'charset': 'utf8mb4',
    'cursorclass': pymysql.cursors.DictCursor
}

def get_and_update_owner():
    """获取 updated_at 最早的在线支持人员并更新其时间，返回 name 和 id"""
    try:
        logger.info("Connecting to database to get support staff...")
        connection = pymysql.connect(**DB_CONFIG)
        with connection:
            with connection.cursor() as cursor:
                # 获取 updated_at 最早的在线支持人员（包括 name 和 id）
                sql = """
                SELECT name, id 
                FROM support_list 
                WHERE status = 'online'
                ORDER BY updated_at ASC 
                LIMIT 1
                FOR UPDATE
                """
                logger.debug(f"Executing SQL query: {sql}")
                cursor.execute(sql)
                result = cursor.fetchone()
                
                if not result:
                    logger.warning("No online support staff found. Using fallback owner.")
                    # 返回默认值（name 和 id）
                    return {
                        'name': os.getenv('DEFAULT_OWNER_NAME', 'user01'),
                        'id': os.getenv('DEFAULT_OWNER_ID', '')
                    }
                
                name = result['name']
                staff_id = result['id']
                logger.info(f"Selected support staff: {name} (ID: {staff_id})")
                
                # 更新该支持人员的 updated_at 时间
                update_sql = """
                UPDATE support_list 
                SET updated_at = %s 
                WHERE name = %s
                """
                current_time = datetime.now()
                logger.debug(f"Executing update: {update_sql} with params: ({current_time}, {name})")
                cursor.execute(update_sql, (current_time, name))
                connection.commit()
                
                logger.info(f"Updated timestamp for {name} to {current_time}")
                return {
                    'name': name,
                    'id': staff_id
                }
                
    except Exception as e:
        logger.error(f"Database error: {str(e)}", exc_info=True)
        # 返回默认值（name 和 id）
        return {
            'name': os.getenv('DEFAULT_OWNER_NAME', 'user01'),
            'id': os.getenv('DEFAULT_OWNER_ID', '')
        }

def send_to_slack(title, owner_info):
    """向 Slack 发送消息，使用新的格式"""
    if not SLACK_WEBHOOK_URL:
        logger.error("Slack Webhook URL not configured. Skipping Slack notification.")
        return False
        
    try:
        # 创建消息内容 - 按照新格式
        # 格式: "Case 01590054 - Medium - People Yun\nOwner: Warner Chen <@U07GJ9QLC2Y>"
        name = owner_info['name']
        staff_id = owner_info['id']
        
        # 如果 staff_id 不为空，添加 Slack mention 格式
        owner_str = f"{name} <@{staff_id}>" if staff_id else name
        
        message = f"{title}\nOwner: {owner_str}"
        logger.info(f"Preparing Slack message: {message}")
        
        # 构建 Slack 请求负载
        payload = {"message": message}
        
        # 记录发送前的详细信息
        logger.info(f"Sending to Slack webhook: {SLACK_WEBHOOK_URL}")
        logger.debug(f"Slack payload: {json.dumps(payload)}")
        
        # 发送请求到 Slack
        response = requests.post(SLACK_WEBHOOK_URL, json=payload)
        
        # 记录响应详细信息
        logger.debug(f"Slack response status: {response.status_code}")
        logger.debug(f"Slack response text: {response.text}")
        
        # 检查响应状态
        if response.status_code == 200:
            logger.info(f"Successfully sent to Slack: {message}")
            return True
        else:
            logger.error(f"Failed to send to Slack. Status: {response.status_code}, Response: {response.text}")
            return False
    
    except Exception as e:
        logger.error(f"Error sending to Slack: {str(e)}", exc_info=True)
        return False

@app.route('/5c2df3d1-3371-47bd-a9cf-1983e9adc18b', methods=['POST'])
def webhook_receiver():
    try:
        # 记录接收到的原始请求
        logger.info("Received webhook request")
        logger.debug(f"Request headers: {dict(request.headers)}")
        
        # 获取 JSON 数据
        data = request.get_json()
        
        if not data:
            logger.warning("No data received in webhook request")
            return jsonify({"error": "No data received"}), 400
        
        # 记录完整的请求数据（调试级别）
        logger.debug(f"Full request data: {json.dumps(data, indent=2)}")
        
        # 提取 title 字段 (event.data.title)
        event_data = data.get('event', {}).get('data', {})
        title = event_data.get('title', 'N/A')
        
        # 记录标题信息
        logger.info(f"Extracted title from webhook: {title}")
        
        # 从数据库获取并更新支持人员
        logger.info("Processing owner assignment...")
        owner_info = get_and_update_owner()  # 现在返回包含 name 和 id 的字典
        
        # 创建处理后的数据对象
        processed_data = {
            "title": title,
            "owner_name": owner_info['name'],
            "owner_id": owner_info['id']
        }
        
        # 输出到控制台
        logger.info(f"Processed data: {processed_data}")
        
        # 发送到 Slack
        logger.info("Sending to Slack...")
        slack_success = send_to_slack(title, owner_info)
        
        # 记录最终结果
        logger.info(f"Webhook processing completed. Slack sent: {slack_success}")
        
        return jsonify({
            "status": "success",
            "processed_data": processed_data,
            "slack_sent": slack_success
        }), 200
    
    except Exception as e:
        logger.error(f"Error processing request: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """健康检查端点"""
    logger.info("Health check received")
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()}), 200

if __name__ == '__main__':
    # 记录启动信息
    logger.info(f"Starting webhook receiver on port {PORT}")
    logger.info(f"Database configuration: {DB_CONFIG}")
    logger.info(f"Slack webhook configured: {bool(SLACK_WEBHOOK_URL)}")
    
    app.run(host='0.0.0.0', port=PORT, debug=False)
