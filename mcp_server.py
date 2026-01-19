from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
import sqlite3
import json
import os
from functools import wraps

# ============ 认证配置 ============
# API Key 从环境变量读取，默认值仅用于开发测试
API_KEY = os.environ.get("MCP_API_KEY", "xm-crm-default-dev-key-2026")

def require_auth(func):
    """装饰器：验证 API Key（仅对 SSE 模式生效）"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        # 尝试获取 HTTP 头（仅 SSE 模式下可用）
        try:
            headers = get_http_headers()
            # 支持两种头格式：X-API-Key 或 Authorization: Bearer xxx
            api_key = headers.get("x-api-key") or headers.get("X-API-Key")
            auth_header = headers.get("authorization") or headers.get("Authorization")
            
            if api_key:
                if api_key != API_KEY:
                    return {"error": "Invalid API Key", "code": 401}
            elif auth_header:
                if auth_header.startswith("Bearer "):
                    token = auth_header[7:]
                    if token != API_KEY:
                        return {"error": "Invalid Bearer Token", "code": 401}
                else:
                    return {"error": "Invalid Authorization format", "code": 401}
            else:
                return {"error": "Missing authentication. Provide X-API-Key header.", "code": 401}
        except Exception:
            # stdio 模式下无法获取 headers，跳过认证（本地可信环境）
            pass
        return func(*args, **kwargs)
    return wrapper

# Initialize FastMCP server
mcp = FastMCP("XiamenBankCrm")

DB_PATH = os.path.join(os.path.dirname(__file__), "bank_data.db")

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@mcp.tool()
@require_auth
def search_customers(
    customer_id: int = None,
    name: str = None,
    id_card: str = None
):
    """
    搜索客户详情。支持按客户ID（精确）、姓名（模糊搜索）或身份证号（精确）查询。
    返回包含基础信息、风险偏好和财务目标的客户列表。
    """
    conn = get_db_connection()
    try:
        base_query = """
        SELECT i.*, p.risk_level, p.wealth_tier, p.life_stage, p.preferred_term_min, 
               p.preferred_term_max, p.investment_preference, p.financial_goals, 
               p.liquidity_need, p.marketing_tags
        FROM customer_info i
        LEFT JOIN customer_persona p ON i.customer_id = p.customer_id
        """
        where_clauses = []
        params = []
        
        if customer_id:
            where_clauses.append("i.customer_id = ?")
            params.append(customer_id)
        
        if name:
            where_clauses.append("i.name LIKE ?")
            params.append(f"%{name}%")
            
        if id_card:
            where_clauses.append("(i.id_card = ? OR i.id_card_masked = ?)")
            params.append(id_card)
            params.append(id_card)

        if where_clauses:
            query = base_query + " WHERE " + " AND ".join(where_clauses)
        else:
            query = base_query + " LIMIT 10" # Default to top 10 if no filters
            
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()

@mcp.tool()
@require_auth
def search_wealth_products(
    product_name: str = None,
    product_code: str = None,
    sales_type: str = None,
    product_type: str = None,
    product_status: str = "在售",
    fund_raising: str = None,
    risk_level: str = None,
    issuer: str = None,
    max_min_purchase: float = None,
    limit: int = 10
):
    """
    全面检索理财产品库。支持基于UI原型页面的所有筛选条件：
    - sales_type: 自营、代销
    - product_type: 结构性存款、理财、基金、保险、资管、信托、商业养老金
    - product_status: 在售、存续 (默认为'在售')
    - fund_raising: 公募、私募
    - risk_level: 低风险、偏低风险、中等风险、偏高风险、高风险
    - issuer: 发行机构 (模糊搜索)
    - max_min_purchase: 最高起售金额 (过滤用户买得起的)
    - product_name: 产品名称 (模糊搜索)
    - product_code: 产品代码
    """
    conn = get_db_connection()
    try:
        query = "SELECT * FROM wealth_products WHERE 1=1"
        params = []
        
        filters = {
            "sales_type": sales_type,
            "product_type": product_type,
            "product_status": product_status,
            "fund_raising": fund_raising,
            "risk_level": risk_level,
            "product_code": product_code
        }
        
        for key, value in filters.items():
            if value:
                query += f" AND {key} = ?"
                params.append(value)
                
        if product_name:
            query += " AND product_name LIKE ?"
            params.append(f"%{product_name}%")
            
        if issuer:
            query += " AND issuer LIKE ?"
            params.append(f"%{issuer}%")

        if max_min_purchase is not None:
            query += " AND min_purchase_amount <= ?"
            params.append(max_min_purchase)
            
        query += f" LIMIT ?"
        params.append(limit)
        
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

@mcp.tool()
@require_auth
def analyze_suitability(customer_id: int, product_code: str):
    """
    自动化执行风险与偏好匹配分析。检查产品风险是否超过客户承受能力，以及金额是否符合起购点。
    """
    results = search_customers(customer_id=customer_id)
    if not results:
        return {"error": "Customer not found"}
    
    customer = results[0]
        
    conn = get_db_connection()
    try:
        product = conn.execute("SELECT * FROM wealth_products WHERE product_code = ?", (product_code,)).fetchone()
        if not product:
            return {"error": "Product not found"}
        
        product = dict(product)
        
        # 1. Risk Matching
        risk_map = {
            "保守型": 1,
            "稳健型": 2,
            "平衡型": 3,
            "成长型": 4,
            "进取型": 5
        }
        prod_risk_map = {
            "低风险": 1,
            "偏低风险": 2,
            "中等风险": 3,
            "偏高风险": 4,
            "高风险": 5
        }
        
        cust_risk_val = risk_map.get(customer['risk_level'], 0)
        prod_risk_val = prod_risk_map.get(product['risk_level'], 99)
        
        risk_match = prod_risk_val <= cust_risk_val
        
        # 2. Compliance (Min Purchase)
        funds_match = customer['available_funds'] >= (product['min_purchase_amount'] or 0)
        
        return {
            "suitability_score": 0.95 if risk_match and funds_match else 0.4,
            "is_risk_compatible": risk_match,
            "is_funds_sufficient": funds_match,
            "customer_risk": customer['risk_level'],
            "product_risk": product['risk_level'],
            "available_funds": customer['available_funds'],
            "min_purchase": product['min_purchase_amount'],
            "recommendation_status": "适配" if risk_match and funds_match else "不建议"
        }
    finally:
        conn.close()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Xiamen Bank Wealth MCP Server")
    parser.add_argument("--transport", choices=["stdio", "sse", "http"], default="stdio", help="Transport mode")
    parser.add_argument("--port", type=int, default=8000, help="Port for SSE mode")
    parser.add_argument("--host", default="0.0.0.0", help="Host for SSE mode")
    
    args = parser.parse_args()
    
    if args.transport in ["sse", "http"]:
        mcp.run(transport=args.transport, host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")
