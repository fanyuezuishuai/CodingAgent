# include <bits/stdc++.h>
using namespace std;

const double INF = 1e100;

int n;
vector<vector<double>> graph;
vector<vector<double>> dp;
int mask = 0;
void dfs(int i){
    if(mask == ((1<<n)-1)){
        return;
    }
    for (int j = 1; j <= n; j++){
        if (mask & (1 << (j-1))) continue;
        int old_mask = mask;
        mask = mask | (1 << (j - 1));
        if (dp[j][mask] > dp[i][old_mask] + graph[i][j]){
            dp[j][mask] = dp[i][old_mask] + graph[i][j];
            dfs(j); //只有距离有优化的时候才继续
        }
        //dfs(j);

        mask = old_mask;
    }
}

int main(){
    cin >> n;
    graph.resize(n+1, vector<double>(n+1));
    dp.resize(n + 1, vector<double>(1<<n, INF));
    dp[0][0] = 0;
    vector<vector<double>>pos(n + 1, vector<double>(2));
    pos[0][0] = 0;
    pos[0][1] = 0;
    for (int i = 0; i < n; i++){
        cin >> pos[i+1][0];
        cin >> pos[i+1][1];
    }
    for (int i = 0; i <= n; i++){
        for (int j = 0; j <= n; j++){
            graph[i][j] = sqrt((pos[i][0] - pos[j][0]) * (pos[i][0] - pos[j][0]) + (pos[i][1] - pos[j][1]) * (pos[i][1] - pos[j][1]));
        }
    }
    dfs(0);
    int end = ((1 << n) - 1);
    double ans = dp[1][end];
    for (int i = 2; i <= n; i++){
        if (ans > dp[i][end]){
            ans = dp[i][end];
        }
    }
    cout << fixed << setprecision(2) << ans << endl;
    return 0;
}