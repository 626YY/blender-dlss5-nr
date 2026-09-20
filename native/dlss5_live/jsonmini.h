// jsonmini.h - a tiny JSON reader/writer for the stdin control protocol.
// Handles objects, arrays, strings, numbers, true/false/null. No escapes beyond \" \\ \n \t \uXXXX(ascii).
#pragma once

#include <cstdlib>
#include <map>
#include <memory>
#include <string>
#include <vector>

struct JVal {
    enum Kind { Null, Bool, Num, Str, Arr, Obj } kind = Null;
    bool b = false;
    double n = 0.0;
    std::string s;
    std::vector<JVal> a;
    std::map<std::string, JVal> o;

    bool has(const std::string& k) const { return kind == Obj && o.count(k) != 0; }
    const JVal* get(const std::string& k) const {
        if (kind != Obj) return nullptr;
        auto it = o.find(k);
        return it == o.end() ? nullptr : &it->second;
    }
    double num(const std::string& k, double def) const {
        const JVal* v = get(k);
        if (!v) return def;
        if (v->kind == Num) return v->n;
        if (v->kind == Bool) return v->b ? 1.0 : 0.0;
        return def;
    }
    bool boolean(const std::string& k, bool def) const {
        const JVal* v = get(k);
        if (!v) return def;
        if (v->kind == Bool) return v->b;
        if (v->kind == Num) return v->n != 0.0;
        return def;
    }
    std::string str(const std::string& k, const std::string& def) const {
        const JVal* v = get(k);
        return (v && v->kind == Str) ? v->s : def;
    }
};

class JParser {
public:
    static bool Parse(const std::string& text, JVal* out) {
        JParser p(text);
        p.ws();
        if (!p.value(out)) return false;
        p.ws();
        return p.i == p.t.size();
    }

private:
    explicit JParser(const std::string& text) : t(text) {}
    const std::string& t;
    size_t i = 0;

    void ws() {
        while (i < t.size() && (t[i] == ' ' || t[i] == '\t' || t[i] == '\r' || t[i] == '\n')) ++i;
    }
    bool value(JVal* v) {
        if (i >= t.size()) return false;
        const char c = t[i];
        if (c == '{') return object(v);
        if (c == '[') return array(v);
        if (c == '"') {
            v->kind = JVal::Str;
            return string(&v->s);
        }
        if (t.compare(i, 4, "true") == 0) { v->kind = JVal::Bool; v->b = true; i += 4; return true; }
        if (t.compare(i, 5, "false") == 0) { v->kind = JVal::Bool; v->b = false; i += 5; return true; }
        if (t.compare(i, 4, "null") == 0) { v->kind = JVal::Null; i += 4; return true; }
        return number(v);
    }
    bool number(JVal* v) {
        const char* start = t.c_str() + i;
        char* end = nullptr;
        const double d = strtod(start, &end);
        if (end == start) return false;
        i += static_cast<size_t>(end - start);
        v->kind = JVal::Num;
        v->n = d;
        return true;
    }
    bool string(std::string* s) {
        if (t[i] != '"') return false;
        ++i;
        s->clear();
        while (i < t.size()) {
            const char c = t[i++];
            if (c == '"') return true;
            if (c == '\\') {
                if (i >= t.size()) return false;
                const char e = t[i++];
                switch (e) {
                    case '"': s->push_back('"'); break;
                    case '\\': s->push_back('\\'); break;
                    case '/': s->push_back('/'); break;
                    case 'n': s->push_back('\n'); break;
                    case 't': s->push_back('\t'); break;
                    case 'r': s->push_back('\r'); break;
                    case 'b': s->push_back('\b'); break;
                    case 'f': s->push_back('\f'); break;
                    case 'u': {
                        if (i + 4 > t.size()) return false;
                        const unsigned cp = static_cast<unsigned>(strtoul(t.substr(i, 4).c_str(), nullptr, 16));
                        i += 4;
                        // utf-8 encode (BMP only; surrogates are rare in our protocol)
                        if (cp < 0x80) s->push_back(static_cast<char>(cp));
                        else if (cp < 0x800) { s->push_back(static_cast<char>(0xC0 | (cp >> 6))); s->push_back(static_cast<char>(0x80 | (cp & 0x3F))); }
                        else { s->push_back(static_cast<char>(0xE0 | (cp >> 12))); s->push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F))); s->push_back(static_cast<char>(0x80 | (cp & 0x3F))); }
                        break;
                    }
                    default: return false;
                }
            } else {
                s->push_back(c);
            }
        }
        return false;
    }
    bool array(JVal* v) {
        v->kind = JVal::Arr;
        ++i;  // [
        ws();
        if (i < t.size() && t[i] == ']') { ++i; return true; }
        while (true) {
            JVal item;
            ws();
            if (!value(&item)) return false;
            v->a.push_back(std::move(item));
            ws();
            if (i >= t.size()) return false;
            if (t[i] == ',') { ++i; continue; }
            if (t[i] == ']') { ++i; return true; }
            return false;
        }
    }
    bool object(JVal* v) {
        v->kind = JVal::Obj;
        ++i;  // {
        ws();
        if (i < t.size() && t[i] == '}') { ++i; return true; }
        while (true) {
            ws();
            std::string key;
            if (!string(&key)) return false;
            ws();
            if (i >= t.size() || t[i] != ':') return false;
            ++i;
            ws();
            JVal item;
            if (!value(&item)) return false;
            v->o[key] = std::move(item);
            ws();
            if (i >= t.size()) return false;
            if (t[i] == ',') { ++i; continue; }
            if (t[i] == '}') { ++i; return true; }
            return false;
        }
    }
};

inline std::string JEscape(const std::string& s) {
    std::string o;
    for (char c : s) {
        if (c == '"') o += "\\\"";
        else if (c == '\\') o += "\\\\";
        else if (c == '\n') o += "\\n";
        else if (c == '\r') o += "\\r";
        else if (c == '\t') o += "\\t";
        else o.push_back(c);
    }
    return o;
}
