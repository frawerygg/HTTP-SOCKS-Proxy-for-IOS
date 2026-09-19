# HTTP/SOCKS Proxy for iPhone

A lightweight HTTP and SOCKS5 proxy that runs directly on an iPhone and lets another computer route traffic through a selected network interface on the phone.

This project was built through human-directed AI development. AI tools handled much of the coding, debugging, testing, and code review, while real-device testing and design decisions were performed manually.

<p align="center">
  <img
    src="https://github.com/user-attachments/assets/9d885b1f-c28d-4f1f-8f14-5005a7ece84f"
    alt="socks5"
    width="300"
  />
</p>

---

# Quick Start

## 1. Connect the phone to your computer

Connect the iPhone using USB, hotspot, or another local network connection.

The computer only needs a local path to reach the phone.

For the USB setup, the connection may create a local address similar to:

```text
169.254.x.x
```

The exact address varies between devices and connections.

---

## 2. Run the proxy on the phone

Run:

```text
socks5.py
```

Choose:

```text
Stable Mode
```

Select the outbound interface you want the proxy to use.

For example, cellular may appear as:

```text
pdp_ip0
```

Wi-Fi may appear as an `en*` interface such as:

```text
en0
```

Interface names can vary, so use the interface information shown by the program rather than assuming a fixed name.

Then press:

```text
Start
```

---

## 3. Find the phone's local IP

Use the local address shown by the proxy.

For a USB/local connection, it may look similar to:

```text
169.254.x.x
```

Do not hard-code this address. Use the address assigned to your current connection.

---

## 4. Configure SOCKS5 on the computer

The default SOCKS5 port is:

```text
9876
```

Configure the computer with:

```text
SOCKS5 Host: <PHONE_LOCAL_IP>
Port: 9876
```

For example:

```text
Host: 169.254.123.45
Port: 9876
```

On macOS:

```text
System Settings
→ Network
→ Active Connection
→ Details
→ Proxies
→ SOCKS Proxy
```

Enter the phone's local IP and port `9876`.

Apply the settings.

That's it.

---

# Default Ports

| Service | Port |
|---|---:|
| SOCKS5 | `9876` |
| HTTP Proxy | `9877` |
| WPAD / PAC | `8088` |

SOCKS5 is recommended for general use.

---

# How It Works

The main idea is simple:

**the connection used to reach the phone does not have to be the same connection the phone uses to reach the Internet.**

A normal connection might look like this:

```text
Mac / PC
    │
    │ USB local link
    ▼
 iPhone
    │
    │ HTTP / SOCKS Proxy
    ▼
Selected iPhone interface
    │
    ▼
 Internet
```

The computer is not asking iOS to directly route all of its traffic.

Instead, the computer connects to a proxy server running on the phone.

When the proxy receives a request, the phone creates a new outbound connection on behalf of the computer.

This produces two separate network paths:

```text
INBOUND

Computer
   │
   │ USB / hotspot / local network
   ▼
iPhone proxy
```

and:

```text
OUTBOUND

iPhone proxy
   │
   │ Selected interface
   ▼
Internet
```

That separation is the important part of the project.

---

## Why USB Is Useful

The USB connection is especially useful when the phone is already connected to a real Wi-Fi router.

Normally, an iPhone does not behave like a general-purpose Wi-Fi repeater where it simply receives Internet from a Wi-Fi router and rebroadcasts that same Wi-Fi connection through Personal Hotspot.

Personal Hotspot is primarily designed around sharing the phone's cellular connection.

That creates a problem if the goal is:

```text
Mac
 ↓
iPhone
 ↓
Real Wi-Fi router
 ↓
Internet
```

The Mac needs a way to reach the phone, but using the phone's Wi-Fi radio as the connection between the Mac and phone can interfere with the phone remaining connected to the real router.

USB provides another local path.

Instead of using Wi-Fi for both sides:

```text
Mac
 ↓
Wi-Fi
 ↓
iPhone
 ↓
Wi-Fi
 ↓
Router
```

the setup becomes:

```text
Mac
 │
 │ USB
 ▼
iPhone
 │
 │ Wi-Fi
 ▼
Router
 │
 ▼
Internet
```

USB is therefore not the Internet connection itself.

It is simply the local link used to deliver proxy requests from the computer to the phone.

The phone's normal Wi-Fi interface can remain available for outbound traffic.

---

## The iOS Wi-Fi Interface

On many iOS configurations, the normal Wi-Fi interface appears as an `en*` interface, commonly something such as:

```text
en0
```

When the phone is connected to a real Wi-Fi router, that interface has the address and route associated with the Wi-Fi network.

The proxy can use that interface's source address when creating outbound connections.

Conceptually:

```text
Mac
 │
 │ USB
 ▼
iPhone local USB interface
 │
 │ proxy receives request
 ▼
HTTP / SOCKS proxy
 │
 │ bind outbound connection
 ▼
en0 / Wi-Fi
 │
 ▼
Wi-Fi Router
 │
 ▼
Internet
```

So the Mac itself does not need direct access to the router's connection through iOS tethering.

It only needs to reach the proxy.

The proxy is already running inside the phone, where the real Wi-Fi interface is available.

---

## Cellular Works the Same Way

The same design can be used with cellular.

For example:

```text
Mac
 │
 │ USB
 ▼
iPhone
 │
 │ SOCKS5
 ▼
pdp_ip0 / Cellular
 │
 ▼
Internet
```

The computer still connects locally over USB.

Only the proxy's outbound interface changes.

This is why the project can expose multiple possible outbound interfaces without changing the computer-side proxy configuration.

---

## Why a Proxy Is Needed

Without a proxy, the computer normally depends on iOS itself to perform routing or tethering.

With the proxy, the computer sends application connections to a program running directly on the phone.

For example, instead of the Mac directly opening:

```text
Mac → example.com
```

it effectively does:

```text
Mac
 ↓
SOCKS5 request
 ↓
iPhone proxy
 ↓
proxy opens example.com
 ↓
Internet
```

The Internet server sees the connection created by the iPhone.

This also allows the program to control which available phone interface is used for that outbound connection.

---

## Source Binding

The proxy does not simply ask iOS to use whichever route happens to be the system default.

For supported connections, it binds outbound TCP, UDP, and DNS traffic to the source address associated with the selected interface.

Conceptually:

```text
Selected interface
        │
        ▼
   Source address
        │
        ▼
Proxy outbound socket
        │
        ▼
     Internet
```

This is what allows the inbound USB connection and outbound Wi-Fi or cellular connection to remain separate.

---

# Security

This project is intended primarily for personal use on trusted local connections.

The proxy may listen on:

```text
0.0.0.0
```

This makes it possible for the service to remain reachable when iOS local interfaces change, but it can also make the proxy reachable by other devices on the same network.

There is currently no proxy authentication.

Do not expose the proxy to an untrusted network unless you understand the consequences.

The project contains no telemetry, analytics, advertising, credential collection, or hidden remote-control service.

---

# License

MIT License.

---

# Disclaimer

This project is provided as-is for networking experimentation and personal proxy use.

Users are responsible for complying with applicable laws, network policies, carrier terms, and service terms.
