# include <bits/stdc++.h>
using namespace std;

bool is_prime(int a){
    if (a < 2) return false;
    for (int i = 2; i * i <= a; i++){
        if (a % i == 0) return false;
    }
    return true;
}

int n, k;
int ans = 0;
int sum = 0;
vector<int> num;

// 现在选到第i个数字
void dfs(int i, int start){
    if (i > k){
        if (is_prime(sum)){
            ans++;
        }
        return;
    }
    for (int j = start; j < n; j++){
        sum += num[j];
        dfs(i+1, j+1);

        sum -= num[j];
        start = j+1;
    }
}

int main(){
    cin >> n >> k;
    num.resize(n);
    for (int i = 0; i < n; i++){
        cin >> num[i];
    }
    dfs(1, 0);
    cout << ans;
    return 0;
}