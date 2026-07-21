# SaathScreen — Watch Party App

Simple Flask + Socket.IO app jisse tum aur tumhara dost kahi se bhi ek saath movie/video dekh sakte ho (synced play/pause/seek) + live chat ke saath.

## Kaise chalayein (apne computer par)

1. Python 3.9+ hona chahiye. Terminal me project folder me jao:
   ```
   cd watchparty
   ```

2. Dependencies install karo:
   ```
   pip install -r requirements.txt
   ```

3. App run karo:
   ```
   python app.py
   ```

4. Browser me kholo: `http://localhost:5000`

## ⚠️ Zaroori baat — dost tak pahuchana

`localhost:5000` sirf tumhare apne computer pe chalega — tumhara dost isko directly open nahi kar payega, chahe room code kuch bhi ho. Do options hain:

**Option A — Quick testing (ngrok se, 5 minute ka kaam)**
1. https://ngrok.com se free account banao aur ngrok install karo
2. App chalao (`python app.py`), phir doosre terminal me: `ngrok http 5000`
3. Ngrok jo link dega (e.g. `https://xxxx.ngrok-free.app`) — wahi link + room code dono dost ko WhatsApp pe bhejo
4. Ye tab tak chalega jab tak tumhara computer aur ngrok chal rahe hain

**Option B — Free hosting (permanent link, computer band karne ke baad bhi chalega)**

## Free me deploy kaise karein (Render.com)

Render sabse aasan free option hai — WebSockets support karta hai (Flask-SocketIO ke liye zaroori), credit card nahi maangta, aur deploy karna simple hai.

1. Is folder (`watchparty/`) ko ek GitHub repo me push karo (agar GitHub account nahi hai to free bana lo)
2. https://render.com par jao, GitHub se sign up karo (free, no card)
3. Dashboard me **New → Web Service** pe click karo, apna repo select karo
4. Settings me:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python app.py` (ye already `Procfile` me bhi likha hai, Render usko khud detect kar lega)
5. **Create Web Service** dabao — 2-3 minute me build hoga
6. Render tumhe ek permanent link dega jaisa `https://saathscreen.onrender.com` — yahi link tum dost ko bhej sakte ho, room code ke saath

**Free tier ki 2 baatein dhyan me rakho:**
- Agar 15 minute tak koi bhi activity nahi hoti (koi visit nahi karta), Render service "sleep" me chala jata hai. Agle visit par wapas jaagne me ~30-60 second lagte hain — pehli baar thoda wait karna padega, phir normal chalega.
- Service sleep hone par server ki memory clear ho jaati hai, matlab jo rooms bane the wo delete ho jaayenge. Agar aisa ho to bas naya room bana lo — 2 second ka kaam hai.

**Alternative:** Railway.app bhi try kar sakte ho — usme cold start (sleep wala issue) nahi hota, lekin free credit limited hai (monthly ~$5 tak).

Deploy hone ke baad **haan, bilkul kaam karega** — jaisa localhost pe chal raha tha waisa hi, bas ab tumhare dost ke liye bhi wahi link kaam karega, kahi se bhi.

## Video kaise dalein

1. Google Drive par video upload karo
2. Share settings me "Anyone with the link" select karo
3. Wo share link copy karke room ke andar "Load Video" box me paste karo
4. Bade files (100MB+) me Drive ka "virus scan warning" aa sakta hai jo direct playback rok deta hai — agar aisa ho to koi choti file try karo ya kisi doosri direct video hosting service (jaise apna server, Dropbox direct link, etc.) use karo

## Kaise use karein
1. Ek banda "Room Banao" pe naam dalke room create karega, usko 6-letter code milega
2. Wo code WhatsApp pe dost ko bhej do (link ke saath, agar ngrok/hosting use kar rahe ho)
3. Dost "Room Join Karo" me naam + code dalke join karega
4. Koi bhi banda video link paste karega, aur jab bhi koi play/pause/seek karega, dusre ke player me automatically wahi hoga
5. Side me chat karte raho movie dekhte dekhte

## Limitations (honestly batana zaroori hai)
- Sync ekdum frame-perfect nahi hoga — network delay ki wajah se 0.5-1 second ka farak aa sakta hai, jo generally movie dekhne ke liye theek hai
- Bahut badi Google Drive files kabhi kabhi load nahi hoti (Drive ki apni limitation)
- Rooms server ki memory me store hote hain — agar server restart hua to sab rooms delete ho jaate hain
