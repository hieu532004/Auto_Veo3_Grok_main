const SECRET = "7c1e4b9a2f6d8c3e1a9f5b2d7c4e8a1f6b3d9c2e7a5f1b4c8d6e2a9f"; // Trùng với LICENSE_SECRET file License.py
const SHEET_NAME = "Trang tính 1"; // Tên sheet

function doPost(e) {
    try {
        const rawData = e.postData.contents;
        const body = JSON.parse(rawData);

        // Đọc params
        const license_key = body.license_key || "";
        const machine_id = body.machine_id || "";
        const ts = parseInt(body.ts) || 0;
        const nonce = body.nonce || "";
        const client_sig = body.sig || "";

        // Xác thực chữ ký
        const req_msg = "license_key=" + license_key + "&machine_id=" + machine_id + "&ts=" + ts + "&nonce=" + nonce;
        const expected_sig = computeHmacSha256(req_msg, SECRET);

        if (client_sig !== expected_sig) {
            return jsonResponse({ ok: false, error: "Sai chữ ký bảo mật" });
        }

        // Kiểm tra request (chống giả mạo trong 5 phút)
        const server_ts = Math.floor(Date.now() / 1000);
        if (Math.abs(server_ts - ts) > 300) {
            return jsonResponse({ ok: false, error: "Request quá hạn" });
        }

        const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
        if (!sheet) {
            return jsonResponse({ ok: false, error: "Không tìm thấy sheet: " + SHEET_NAME });
        }

        const data = sheet.getDataRange().getDisplayValues();

        let foundRow = -1;
        let rowData = null;

        for (let i = 1; i < data.length; i++) {
            if (data[i][0] == license_key) {
                foundRow = i;
                rowData = data[i];
                break;
            }
        }

        if (foundRow === -1) {
            return jsonResponse(signResponse(false, license_key, machine_id, 0, "{}", server_ts, nonce, "Key không tồn tại"));
        }

        let db_machine_id = data[foundRow][1] ? String(data[foundRow][1]).trim() : "";
        let expiration = data[foundRow][2];
        let owner_name = data[foundRow][3] || "";
        let owner_phone = data[foundRow][4] || "";
        let status = data[foundRow][5] ? String(data[foundRow][5]).trim().toLowerCase() : "";

        if (status !== "active") {
            return jsonResponse(signResponse(false, license_key, machine_id, 0, "{}", server_ts, nonce, "Key đã bị khóa hoặc chưa kích hoạt"));
        }

        // Kiểm tra Machine ID
        if (!db_machine_id) {
            // Lần đầu kích hoạt, thiết lập Machine ID
            sheet.getRange(foundRow + 1, 2).setValue(machine_id);
            db_machine_id = machine_id;
        } else if (db_machine_id !== machine_id) {
            return jsonResponse(signResponse(false, license_key, machine_id, 0, "{}", server_ts, nonce, "Key đã được kích hoạt trên PC khác"));
        }

        // Kiểm tra Hạn sử dụng (Expiration định dạng YYYY-MM-DD)
        let expires_at = 0;
        if (expiration) {
            let d = new Date(expiration);
            if (!isNaN(d.getTime())) {
                expires_at = Math.floor(d.getTime() / 1000);
                if (expires_at < server_ts) {
                    return jsonResponse(signResponse(false, license_key, machine_id, expires_at, "{}", server_ts, nonce, "Key đã hết hạn"));
                }
            } else {
                expires_at = server_ts + 3600 * 24 * 3650;
            }
        } else {
            // Vĩnh viễn (10 năm)
            expires_at = server_ts + 3600 * 24 * 3650;
        }

        const features = JSON.stringify({
            name: owner_name,
            sdt: owner_phone
        });

        return jsonResponse(signResponse(true, license_key, machine_id, expires_at, features, server_ts, nonce, ""));

    } catch (e) {
        return jsonResponse({ ok: false, error: "Lỗi Server: " + e.message });
    }
}

function signResponse(ok, license_key, machine_id, expires_at, features, server_ts, nonce, errorStr) {
    const ok_str = ok ? "true" : "false";
    const msg = "ok=" + ok_str + "&license_key=" + license_key + "&machine_id=" + machine_id + "&expires_at=" + expires_at + "&server_ts=" + server_ts + "&nonce=" + nonce;
    const sig = computeHmacSha256(msg, SECRET);

    return {
        ok: ok,
        ACTIVE: ok,
        license_key: license_key,
        machine_id: machine_id,
        expires_at: expires_at,
        server_ts: server_ts,
        nonce: nonce,
        features: features,
        server_sig: sig,
        error: errorStr,
        reason: errorStr
    };
}

function computeHmacSha256(message, secret) {
    var signature = Utilities.computeHmacSha256Signature(message, secret);
    return signature.map(function (byte) {
        return ('0' + (byte & 0xFF).toString(16)).slice(-2);
    }).join('');
}

function jsonResponse(obj) {
    return ContentService.createTextOutput(JSON.stringify(obj))
        .setMimeType(ContentService.MimeType.JSON);
}

function doGet(e) {
    return ContentService.createTextOutput("Hệ thống License Server đang chạy.");
}
