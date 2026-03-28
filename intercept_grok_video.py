import asyncio
import json
from playwright.async_api import async_playwright
import os

USER_DATA_DIR = r"D:\Private_project\autoVeo3_Grok-main\chrome_user_data\Chrome-auto"

async def main():
    print("🚀 Đang mở trình duyệt Chrome chứa tài khoản Grok của bạn...")
    try:
        async with async_playwright() as p:
            browser_context = await p.chromium.launch_persistent_context(
                USER_DATA_DIR,
                headless=False,
                channel="chrome" if os.name == "nt" else None,
                args=["--disable-blink-features=AutomationControlled"]
            )
            
            page = await browser_context.new_page()
            
            print("================================================================")
            print("🟢 Trình duyệt đã mở. Vui lòng vào grok.com và tạo video có sử dụng ảnh nhân vật.")
            print("📡 Hệ thống đang lắng nghe dữ liệu mạng (Network)...")
            print("================================================================")

            async def handle_request(request):
                # Chỉ bắt các request tạo hội thoại mới (có thể chứa payload tạo video)
                if "conversations/new" in request.url and request.method == "POST":
                    print(f"\n[🚀 BẮT ĐƯỢC REQUEST TẠO LỆNH] {request.url}")
                    try:
                        post_data = request.post_data
                        if post_data:
                            json_data = json.loads(post_data)
                            
                            # Ghi ra file để AI phân tích
                            with open("grok_video_payload_debug.json", "w", encoding="utf-8") as f:
                                json.dump(json_data, f, indent=4, ensure_ascii=False)
                            
                            print("✅ Đã trích xuất JSON payload tạo video thành công vào file 'grok_video_payload_debug.json'!")
                            print("--------------------------------------------------")
                            print("Nội dung Payload:")
                            # In ra các khóa quan trọng
                            print(f"- message: {json_data.get('message')}")
                            print(f"- modelName: {json_data.get('modelName')}")
                            print(f"- fileAttachments: {json_data.get('fileAttachments')}")
                            model_map = json_data.get('responseMetadata', {}).get('modelConfigOverride', {}).get('modelMap', {})
                            print(f"- modelMap: {json.dumps(model_map, indent=2)}")
                            print("--------------------------------------------------")
                    except Exception as e:
                        print(f"❌ Lỗi đọc payload: {e}")

            page.on("request", handle_request)
            
            await page.goto("https://grok.com/")
            
            # Giữ cho trình duyệt mở để bạn thao tác. Dừng bằng Ctr+C trong console hoặc khi đóng cửa sổ
            while len(browser_context.pages) > 0:
                await asyncio.sleep(1)

    except BaseException as e:
        print(f"⚠️ Đã đóng tool bắt Request: {e}")

if __name__ == "__main__":
    asyncio.run(main())
