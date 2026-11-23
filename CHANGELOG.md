# Changelog

Định dạng theo [Keep a Changelog](https://keepachangelog.com/vi/1.1.0/); phiên bản theo [Semantic Versioning](https://semver.org/lang/vi/).

## Unreleased

### Thêm

- Bổ sung tệp [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) và cập nhật ghi công đầy đủ các nguồn upstream, tác giả, bản vá, font và thư viện phụ thuộc trong `LICENSE`, `README.md`, `README.vi.md`, `pyproject.toml`, `native/chromium-148/README.md` và `chrome_engine/README.md`.

### Thay đổi

- Chụp lại toàn bộ 12 ảnh giao diện **RonC Profile** (EN + VI) trong `docs/images/`, đồng bộ mô tả nhãn trạng thái trong `README.md` / `README.vi.md`, và dọn các comment `PionLogin` cũ trong mã nguồn giao diện.

### Sửa

- App desktop: nhấn Esc để đóng hộp thoại quản lý và sửa profile, nhấp đúp bút chì chỉ mở một hộp thoại, và nhấn Enter trong ô tên để tạo hoặc lưu profile.

- App desktop: cập nhật đồng thời số lượng Kho Proxy ở thanh bên và chân trang, giữ nội dung dài trong cột, cho phép hủy xác nhận xóa bằng Esc và giữ các nút đứng yên khi hiện thông báo lưu.

- Sau khi app hoặc server bị buộc tắt, Chromium tự đóng an toàn để có thể mở lại, dừng hoặc xoá profile sau khi khởi động lại mà không mất dữ liệu.

- Nhiều tiến trình dùng chung dữ liệu: hai lần sửa tiện ích cùng lúc không còn mất một thay đổi; Dừng ngâm tài khoản từ tiến trình khác thật sự dừng lượt chạy, tiến trình khác không chạy trùng tác vụ đang chạy; `GET /profiles/<name>` và MCP `get_profile` thấy ngay profile tiến trình khác vừa tạo. Thanh thao tác hàng loạt cho biết số profile đã chọn đang bị bộ lọc ẩn, và menu Giao diện/Ngôn ngữ trên web vừa màn hình 320 px.

- Xuất ZIP profile Chromium đang chạy không còn vướng file khoá hay để lại bản sao lưu dở; các tiến trình không ghi đè proxy của nhau; cookie Netscape giữ nguyên giá trị rỗng; bộ tạo cũ và ô chọn CPU dùng số nhân phù hợp với GPU Apple Silicon.

- Không chạy lịch ngâm tài khoản đã bị xoá từ tiến trình khác; web giữ ghi chú nhiều dòng và tiêu điểm ô chọn, đồng thời chỉ tải lại danh sách một lần khi mở hoặc dừng nhiều profile.

- Tác vụ ngâm tài khoản giữ đúng profile khi đổi tên và không treo ở trạng thái đang chạy khi không lưu được; web cập nhật fingerprint theo danh sách, hiển thị đúng cổng proxy và cho phép sửa URL mở mặc định.

- Profile Chrome mới mặc định theo hệ điều hành của máy; lỗi mở trình duyệt hiển thị lý do dễ hiểu bằng đúng ngôn ngữ trên desktop và web, tooltip giữ lý do thất bại và nhãn phân trang rõ ràng hơn. Trên macOS, mặc định tắt Impeller để tooltip không bị lỗi chữ, vẫn giữ giá trị `FLET_NO_IMPELLER` do người dùng đặt.

- Chromium 148: phát hiện patch bị thiếu khi bỏ qua chuỗi patch đã áp dụng và đối chiếu font được bảo vệ với danh sách cài trên máy, tránh bỏ sót font bị ẩn ở cả hai seed.
- Hộp thoại tiện ích hiển thị trạng thái trống, GPU Chrome khớp fingerprint đã lưu, bảng màu không tràn khi mở menu ngôn ngữ và hộp thoại Cookie co vừa nội dung.
- Bố cục desktop tự xuống dòng khi thu nhỏ cửa sổ, giữ tìm kiếm, thao tác profile và phân trang luôn trong khung nhìn.
- Hộp thoại Ngâm TK hiển thị và ghi log lỗi khi không lưu được tác vụ, không chạy tác vụ hay để lại lịch chưa lưu trong bộ nhớ.
- Cảnh báo engine Chromium trong hộp thoại tạo profile giờ đọc được ở giao diện tối sau khi đổi giao diện.
- Import/export cookie của profile Chrome dùng core đã gắn với profile và giữ `profile_lease`, không yêu cầu cài thêm core mặc định.
- Lưu cài đặt tiện ích thất bại sẽ khôi phục cấu hình cũ và hiển thị lỗi, không còn báo đã lưu nhầm.
- Cài lại tiện ích thất bại khi lưu sẽ giữ nguyên phiên bản và dữ liệu tiện ích trước đó.
- Cài nhiều tiện ích trên desktop hiển thị kết quả từng file, không còn che mất lỗi khi có file cài thành công.
- Gán proxy từ pool chỉ báo thành công sau khi lưu profile (`strict=True`); nếu ghi thất bại, khôi phục proxy cũ trong bộ nhớ và hiển thị lỗi trên giao diện thay vì crash.
- File ZIP xuất profile không còn chứa chính nó khi lưu trong thư mục dữ liệu của profile.
- Import ZIP kiểm tra `profile.json` là JSON object và `os_type` là chuỗi hợp lệ trước khi lưu, tránh làm lỗi danh sách profile.
- App desktop: ô chọn số dòng mỗi trang trong danh sách profile hiển thị đầy đủ số đã chọn.
- App desktop: Import proxy vào pool khi không ghi được file proxy hiện lỗi ngay trong hộp thoại, giữ nguyên danh sách nhập và pool như trước thay vì im lặng không báo gì.
- App desktop: Lỗi nhập profile trùng tên và JSON cookie sai định dạng hiển thị đúng ngôn ngữ giao diện, không lộ chi tiết bộ phân tích JSON.
- Ngâm TK: Khi không lưu được thao tác xoá hoặc dừng tác vụ, giữ nguyên danh sách và trạng thái tác vụ, đồng thời hiển thị lỗi trong hộp thoại.
- Khởi động trình duyệt chỉ đếm các tab khôi phục thành công; nếu tất cả đều lỗi, mở URL khởi động đã cấu hình và không để lại tab trống thừa.
- API: PATCH profile với notes, account_uid, start_url hoặc tags là null sẽ xoá giá trị đã lưu; trường không gửi vẫn được giữ nguyên.
- Gán tiện ích không còn ghi đè tiện ích vừa lưu khi sửa profile; cửa sổ desktop khi mở trên macOS nằm giữa và vừa vùng hiển thị.

## 2.4.0 - 2026-10-30

### Thay đổi
- Engine Chrome không còn là thử nghiệm: bỏ nhãn "experimental"/"thử nghiệm" trong app, web panel, API và MCP. README có mục "Giới hạn đã biết" (font lấy từ máy, kích thước màn hình thật khi chạy có cửa sổ, không có Widevine, ghim bản 148, WebRTC khi có proxy).
- Locale tự động được chọn một lần cho mỗi quốc gia rồi giữ cố định theo profile (file `geo_locale.json` trong thư mục profile), trên cả Camoufox và Chrome. Trước đây locale bị chọn lại ngẫu nhiên ở mỗi lần mở, nên cùng một tài khoản có thể hôm nay là `en-US`, hôm sau là `es-US`. Locale này đi kèm khi xuất profile và bị xoá khi tạo fingerprint mới.
- Kiểm tra IP không còn cảnh báo "locale ngẫu nhiên"; khi kiểm tra theo profile (`/profiles/{name}/ip-check`, MCP `check_profile_ip`) kết quả hiện locale mà profile đang giữ.
- Ngôn ngữ giao diện mặc định của app desktop và web panel là English; đặt `FOXPROFILE_LANG=vi` (hoặc bấm "Tiếng Việt" trong app) để dùng tiếng Việt.

### Sửa
- App desktop: nút "Launch" không còn bị gãy chữ thành hai dòng khi dùng giao diện English.
- Proxy dạng `host:port:user:pass` có `@` hoặc `:` trong mật khẩu không còn bị bỏ qua (trước đây profile mở mà không qua proxy). Chấp nhận proxy IPv6 dạng `[2001:db8::1]`; host proxy tối đa 253 ký tự.
- Tên profile không phân biệt hoa thường: "Demo" và "demo" không còn tạo được hai profile dùng chung một thư mục dữ liệu trên Windows/macOS. Đổi tên chỉ khác hoa thường vẫn giữ nguyên dữ liệu; tên chứa ký tự điều khiển bị từ chối.
- Đổi tên profile sang tên có thư mục dữ liệu sót lại trả về lỗi 409 thay vì làm hỏng danh sách profile. `profiles.json` được ghi nguyên tử, và các thao tác đồng thời (API, MCP, app) không còn ghi đè lẫn nhau, gây lỗi 500 hay xoá nhầm thư mục của profile vừa tạo lại.
- Proxy không vào được mạng báo lỗi dễ hiểu ("no internet connection through proxy host:port") thay vì chuỗi lỗi nội bộ; chi tiết vẫn có trong log.
- MCP `update_profile(proxy="")` xoá được proxy.
- Điều khiển trang từ chối cả scheme bị mã hoá phần trăm (`%66ile:`) và URL hỏng như `[::1`.
- Web panel: xác nhận "New fingerprint" không còn đóng luôn hộp thoại cookie; Esc đóng màn hình xem trực tiếp và đúng hộp thoại trên cùng; hiện lỗi kèm nút Thử lại khi không kết nối được server thay vì trang trắng; nút Lưu và các thao tác cookie bị khoá khi đang chạy; lỗi 422 hiển thị dễ đọc.
- App desktop: hỏi xác nhận trước khi tạo fingerprint mới; ô chọn ở tiêu đề bảng không còn bỏ chọn các profile ở trang khác; rail hiện số profile và số trình duyệt đang chạy; hộp thoại xoá nói rõ cookie và dữ liệu sẽ mất vĩnh viễn.

## 2.3.1 - 2026-10-09

### Sửa
- Web panel: bảng profile không còn tràn mất cột thao tác (Mở/Dừng) khi cửa sổ trình duyệt hẹp hơn khoảng 1460px; bảng giờ đổi bố cục theo bề rộng thực của chính nó.
- Profile không còn kẹt ở trạng thái "Đang mở" trên app desktop khi cùng lúc được mở qua API hoặc MCP; hai lệnh mở đồng thời cho cùng một profile không còn khởi chạy hai trình duyệt.

## 2.3.0 - 2026-10-09

### Thay đổi
- Thiết kế lại giao diện app desktop và web panel theo một hệ thống chung (xem `DESIGN.md`): nền giấy sáng, rail xanh, bảng profile thẳng cột với cột Thiết bị / Proxy / Timezone · Locale, và "dấu" trạng thái (đang chạy, đang mở, lỗi) phân biệt bằng cả hình dạng lẫn màu. Font Be Vietnam Pro và JetBrains Mono được đóng gói sẵn (OFL), chạy được không cần mạng.
- Thêm bộ lọc profile kèm số đếm (tất cả, đang chạy, có/không proxy, engine Chrome) trên cả hai giao diện; web panel có thêm thao tác hàng loạt và hộp thoại xác nhận riêng; app desktop đổi được Tiếng Việt / English ngay khi đang chạy.
- Thuật ngữ chuyên ngành giữ tiếng Anh trong giao diện tiếng Việt (fingerprint, engine, timezone, locale).

### Thêm
- Nhân **Chrome (thử nghiệm)** bên cạnh Camoufox, dựa trên fingerprint-chromium: chọn khi tạo profile trên app, web panel, REST API (`"engine": "chrome"`) và MCP (`create_profile(engine="chrome")`). Nhân cố định sau khi tạo. Cookie, fingerprint, điều khiển trang, mở lại tab và Kiểm tra IP dùng được như profile Camoufox. Bản trình duyệt được tự tải ở nền khi tạo profile Chrome đầu tiên, hoặc lúc mở profile nếu chưa có (cũng tải tay được bằng `python -m chrome_engine fetch`).
- Proxy dạng `host:port:user:pass` (định dạng thường gặp của nhà bán proxy) được chấp nhận ở mọi nơi nhập proxy, kể cả có tiền tố `socks5://`.
- Mở lại các tab đang mở khi profile được dừng (lưu vào `camoufox_data/<profile>/tabs.json` mỗi 2 giây khi tab thay đổi, chỉ `http`/`https`, tối đa 30 tab). Tắt bằng `FOXPROFILE_RESTORE_TABS=false`.

### Sửa
- Rò rỉ DNS khi dùng proxy `socks5://`/`socks4://`: việc tra IP đi ra lúc mở profile và nút **Kiểm tra IP** từng phân giải tên miền bằng DNS của máy (lộ nhà mạng thật). Nay chuyển sang `socks5h://`/`socks4a://` để proxy phân giải. Bản thân trình duyệt không bị ảnh hưởng.
- Chế độ `HEADLESS=virtual` (server Linux) không còn bỏ sót tiến trình Xvfb khi profile mở lỗi (ví dụ proxy không vào được) hoặc khi runner bị kill cứng/crash. Runner tự quản lý màn hình ảo và ghi lại Xvfb nó đã bật (theo PID và thời điểm khởi động, trong thư mục tạm riêng của user) để launcher, hoặc lần mở sau, tắt đúng Xvfb đó.

## 2.2.0 - 2026-10-08

### Thêm
- Đặt cố định múi giờ (IANA) và ngôn ngữ (`vi-VN`) cho từng profile; để trống vẫn tự động theo IP.
- Nút **Kiểm tra IP**: đối chiếu IP đi ra với Cloudflare, ipinfo và ip-api, cảnh báo lệch quốc gia/múi giờ, IP datacenter, proxy dual-stack và ngôn ngữ ngẫu nhiên. Có trên app, web panel, REST API (`GET /profiles/{name}/ip-check`, `POST /proxy/geo-check`) và MCP (`check_profile_ip`).
- Hướng dẫn triển khai lên server Linux (`docs/DEPLOY.md`, `deploy/`) và các script e2e thủ công (`tests/e2e/`).

## 2.1.0 - 2026-10-08

### Thêm
- MCP server (27 tool) cho AI agent, qua stdio và qua HTTP tại `/mcp`.
- Web panel có token bảo vệ, xem và điều khiển màn hình trình duyệt từ xa.
- Server mode chạy ẩn trình duyệt (`python -m src.server`).
- Hộp thoại hướng dẫn kết nối MCP cho Claude Code, Cursor, VS Code và Claude Desktop.

### Sửa
- Server mode đọc đúng `FOXPROFILE_HEADLESS` từ `.env`.

## 2.0.0 - 2026-10-08

Phát hành đầu tiên của FoxProfile: vân tay cố định theo profile, proxy riêng, xuất/nhập cookie (JSON và Netscape), REST API, giao diện tiếng Việt/tiếng Anh.
