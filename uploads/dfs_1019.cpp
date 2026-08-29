# include <bits/stdc++.h>
using namespace std;

int n;
vector<string> words;
string dragon;
int ans = 1;
int sum = 1;
vector<int> visited;// range0~2, 表示访问次数. 不可超过2

// 两个字符串能否接龙, 返回接龙子字符串的长度, 若不能接龙返回0
int combine(string a, string b){
    int lena = a.size();
    int lenb = b.size();
    int max_len = min(lena, lenb);
    for (int i = 1; i <= max_len; i++){
        if (a.substr(a.size()-i, i) == b.substr(0, i)){
            //cout << "combine " << a << '+' << b << "=" << i; // ////////
            return i;
        }
    }
    //cout << "combine " << a << '+' << b << "=" << 0; // ////////
    return 0;
}

void dfs(){
    bool no_change = true;

    for (int i = 0; i < n; i++){
        if (visited[i] >= 2) continue;
        int sub_len = combine(dragon, words[i]);
        if (sub_len == 0) continue;
        no_change = false;
        visited[i]++;
        string add_word = words[i].substr(sub_len, words[i].size() - sub_len);
        sum += add_word.size();
        ans = max(ans, sum);
        dragon = dragon + add_word;
        dfs();
        dragon = dragon.substr(0, dragon.size() - add_word.size());
        sum -= add_word.size();
        visited[i]--;
    }
    if (no_change) return;
}


int main(){
    cin >> n; 
    words.resize(n);
    visited.resize(n, 0);
    for (int i = 0; i < n; i++){
        cin >> words[i];
    }
    cin >> dragon;
    dfs();
    cout << ans;
    return 0;
}